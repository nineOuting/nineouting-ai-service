#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
수신 서비스: 회의별 상태를 들고, 백엔드에서 온 회의 알림과 줄 묶음을 요약 코드에 넘긴다.

FastAPI 없이 표준 라이브러리만 쓴다 (server/main.py 가 이 클래스를 HTTP로 감싼다).

흐름
  받기(바로 응답) : 준비 확인 -> 형식 검사 -> 묶음 번호 확인 -> 회의별 대기열에 넣기
  처리(뒤에서)    : 회의마다 일꾼 스레드 하나가 대기열을 순서대로 꺼내 요약 코드에 넘기고,
                   요약이 바뀌면 백엔드로 보낸다 (publisher.py)

묶음 번호 규칙 (입력 형식 문서 7·8장)
  이미 받은 번호      -> 무시 (duplicate)
  다음 번호(마지막+1) -> 받음
  그 밖의 번호        -> 처리하지 않고 BatchOutOfOrder(expected). 백엔드가 expected 번부터 다시 보낸다.
  재시작해서 모르는 회의의 묶음도 같은 길로 처리한다 (expected 는 1).

로그 규칙: 전사 내용은 로그에 남기지 않는다. 회의 번호, 묶음 번호, 개수, 예외 이름만 남긴다.
"""
import json
import logging
import os
import queue
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY_DIR = os.path.abspath(os.path.join(HERE, "..", "summary"))
if SUMMARY_DIR not in sys.path:
    sys.path.insert(0, SUMMARY_DIR)

from input_adapter import (adapt_batch, adapt_event, adapt_agendas, adapt_corrections, adapt_summary_edits,  # noqa: E402
                           AdapterError)
from meeting_live_summary_v2 import LiveSummarizer, Cfg  # noqa: E402
from agenda_classifier import AgendaClassifier, ClassifyCfg  # noqa: E402
from agenda_summarizer import AgendaSummarizer, SummarizeCfg  # noqa: E402
from agenda_status import AgendaStatusJudge, StatusCfg  # noqa: E402
from .publisher import AGENDA_CLASSIFICATION, AGENDA_BOARD  # noqa: E402

log = logging.getLogger("ai-service")

SUPPORTED_PROVIDERS = ("echo", "gemini", "anthropic", "openai")   # bedrock 은 다음 작업에서 추가
PROVIDER_KEYS = {                                                 # provider 별로 필요한 API 키 환경변수 (하나만 있으면 됨)
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}


class ConfigError(ValueError):
    """환경변수 설정이 잘못됐을 때."""


class BatchOutOfOrder(Exception):
    """묶음 번호가 이어지지 않을 때. expected 번부터 다시 보내야 한다 (HTTP 409)."""

    def __init__(self, expected):
        super().__init__(f"expectedBatch={expected}")
        self.expected = expected


class NotReady(Exception):
    """아직 받을 수 없을 때 (HTTP 503). 백엔드는 보낼 것을 남겨 두고 나중에 다시 보낸다."""


def cfg_from_env(env=None):
    """환경변수 -> 요약기 설정.
    LLM_PROVIDER (기본 echo = 모델을 부르지 않는 테스트용), LLM_MODEL,
    WINDOW_SEC, REFRESH_SEC, MAX_CALLS_PER_MEETING
    """
    env = os.environ if env is None else env
    provider = env.get("LLM_PROVIDER", "echo")
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"지원하지 않는 LLM_PROVIDER: {provider} (가능: {', '.join(SUPPORTED_PROVIDERS)})")
    kw = {"provider": provider, "model": env.get("LLM_MODEL") or None}
    for name, key in (("window_sec", "WINDOW_SEC"), ("refresh_sec", "REFRESH_SEC"), ("max_calls", "MAX_CALLS_PER_MEETING")):
        if env.get(key):
            try:
                kw[name] = int(env[key])
            except ValueError:
                raise ConfigError(f"{key} 는 정수여야 함: {env[key]}")
    return Cfg(**kw)


def classify_cfg_from_env(env=None, cfg=None):
    """환경변수 -> 안건 분류 설정. 모델은 요약과 같은 LLM_PROVIDER, LLM_MODEL 을 쓴다.
    CLASSIFY_REFRESH_SEC(모델을 부르는 간격, 기본 30초), MAX_CLASSIFY_CALLS_PER_MEETING(회의당 호출 상한, 기본 200)
    """
    env = os.environ if env is None else env
    cfg = cfg or cfg_from_env(env)
    kw = {"provider": cfg.provider, "model": cfg.model}
    for name, key in (("refresh_sec", "CLASSIFY_REFRESH_SEC"), ("max_calls", "MAX_CLASSIFY_CALLS_PER_MEETING")):
        if env.get(key):
            try:
                kw[name] = int(env[key])
            except ValueError:
                raise ConfigError(f"{key} 는 정수여야 함: {env[key]}")
    return ClassifyCfg(**kw)


def summarize_cfg_from_env(env=None, cfg=None):
    """환경변수 -> 안건별 요약 설정. 모델은 요약과 같은 LLM_PROVIDER, LLM_MODEL 을 쓴다.
    AGENDA_SUMMARY_REFRESH_SEC(새 줄이 1개일 때 기다리는 시간, 기본 60초), MAX_AGENDA_SUMMARY_CALLS_PER_MEETING(회의당 호출 상한, 기본 300)
    """
    env = os.environ if env is None else env
    cfg = cfg or cfg_from_env(env)
    kw = {"provider": cfg.provider, "model": cfg.model}
    for name, key in (("refresh_sec", "AGENDA_SUMMARY_REFRESH_SEC"), ("max_calls", "MAX_AGENDA_SUMMARY_CALLS_PER_MEETING")):
        if env.get(key):
            try:
                kw[name] = int(env[key])
            except ValueError:
                raise ConfigError(f"{key} 는 정수여야 함: {env[key]}")
    return SummarizeCfg(**kw)


def status_cfg_from_env(env=None, cfg=None):
    """환경변수 -> 안건 상태 판정 설정. 모델은 요약과 같은 LLM_PROVIDER, LLM_MODEL 을 쓴다.
    AGENDA_STATUS_REFRESH_SEC(새 줄이 1개일 때 기다리는 시간, 기본 60초), MAX_AGENDA_STATUS_CALLS_PER_MEETING(회의당 호출 상한, 기본 300)
    """
    env = os.environ if env is None else env
    cfg = cfg or cfg_from_env(env)
    kw = {"provider": cfg.provider, "model": cfg.model}
    for name, key in (("refresh_sec", "AGENDA_STATUS_REFRESH_SEC"), ("max_calls", "MAX_AGENDA_STATUS_CALLS_PER_MEETING")):
        if env.get(key):
            try:
                kw[name] = int(env[key])
            except ValueError:
                raise ConfigError(f"{key} 는 정수여야 함: {env[key]}")
    return StatusCfg(**kw)


class MeetingState:
    def __init__(self, meeting_id, cfg, llm=None, classify_cfg=None, classify_llm=None, summarize_cfg=None, summarize_llm=None,
                 status_cfg=None, status_llm=None):
        self.meeting_id = meeting_id
        self.id_raw = meeting_id            # 백엔드가 보낸 meetingId 의 원래 값(숫자일 수 있음). 돌려보내는 본문에 쓴다
        self.summarizer = LiveSummarizer(meeting_id, cfg, llm=llm, verbose=False)
        self.classifier = AgendaClassifier(meeting_id, classify_cfg, llm=classify_llm)
        self.agenda_summarizer = AgendaSummarizer(self.classifier, summarize_cfg, llm=summarize_llm)   # 분류 결과를 읽어 안건별로 요약
        self.status_judge = AgendaStatusJudge(self.classifier, status_cfg, llm=status_llm)   # 분류 결과를 읽어 안건 상태를 판정
        self.cls_version = 0                # 백엔드로 보낸 안건 분류 결과의 마지막 version
        self.status = "new"                 # new -> running <-> paused -> ended
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.worker = None
        self.max_batch = 0                  # 지금까지 받은 마지막 묶음 번호 (1부터 빠짐없이 이어진다)
        self.out_of_order = 0               # 번호가 이어지지 않아 다시 보내 달라고 한 횟수
        self.seen_events = set()            # (event, atMs)
        self.events = []                    # [{"event", "atMs"}]
        self.warnings = 0
        self.counts = {"batches": 0, "lines": 0, "edits": 0}
        self.errors = 0
        self.version = 0                    # 백엔드로 보낸 요약의 마지막 version
        self.last_sig = None                # 마지막으로 보낸 요약의 내용 (같으면 다시 보내지 않음)


class Registry:
    def __init__(self, cfg, llm=None, publisher=None, env=None, classify_cfg=None, classify_llm=None,
                 summarize_cfg=None, summarize_llm=None, status_cfg=None, status_llm=None):
        self.cfg = cfg
        self.llm = llm                      # 시험용: 모델 호출 대신 쓸 함수
        self.publisher = publisher
        self.env = os.environ if env is None else env
        self.classify_cfg = classify_cfg or classify_cfg_from_env(self.env, cfg)
        self.classify_llm = classify_llm    # 시험용: 안건 분류에서 모델 호출 대신 쓸 함수
        self.summarize_cfg = summarize_cfg or summarize_cfg_from_env(self.env, cfg)
        self.summarize_llm = summarize_llm  # 시험용: 안건별 요약에서 모델 호출 대신 쓸 함수
        self.status_cfg = status_cfg or status_cfg_from_env(self.env, cfg)
        self.status_llm = status_llm        # 시험용: 안건 상태 판정에서 모델 호출 대신 쓸 함수
        self.shutting_down = False
        self.meetings = {}
        self.lock = threading.Lock()

    # ---------------- 준비 상태 ----------------
    def ready(self):
        """(준비됨, 이유). 종료 중이거나 필요한 API 키가 없으면 준비되지 않은 것으로 본다."""
        if self.shutting_down:
            return False, "서버 종료 중"
        keys = PROVIDER_KEYS.get(self.cfg.provider)
        if keys and self.llm is None and not any(self.env.get(k) for k in keys):
            return False, f"{self.cfg.provider} API 키가 설정되지 않음 ({' 또는 '.join(keys)})"
        return True, None

    def _require_ready(self):
        ok, why = self.ready()
        if not ok:
            raise NotReady(why)

    # ---------------- 받기 ----------------
    def _get(self, meeting_id, raw_id=None):
        with self.lock:
            m = self.meetings.get(meeting_id)
            if m is None:
                m = self.meetings[meeting_id] = MeetingState(meeting_id, self.cfg, self.llm, self.classify_cfg, self.classify_llm,
                                                             self.summarize_cfg, self.summarize_llm,
                                                             self.status_cfg, self.status_llm)
                if raw_id is not None:
                    m.id_raw = raw_id
            return m

    @staticmethod
    def _check_same_id(path_id, body_id):
        if path_id != body_id:
            raise AdapterError(f"meetingId가 주소와 다름 (주소 {path_id}, 본문 {body_id})")

    def receive_batch(self, path_meeting_id, msg):
        self._require_ready()
        a = adapt_batch(msg)                                   # 형식이 틀리면 AdapterError
        self._check_same_id(path_meeting_id, a["meeting_id"])
        m = self._get(a["meeting_id"], a.get("meeting_id_raw"))
        n = a["seq"]
        with m.lock:
            if m.status == "ended":
                log.warning("종료된 회의의 묶음 무시 meeting=%s batch=%s", m.meeting_id, n)
                return {"accepted": False, "result": "ignored", "reason": "ended", "batch": n}
            if n <= m.max_batch:
                log.info("중복 묶음 무시 meeting=%s batch=%s", m.meeting_id, n)
                return {"accepted": True, "result": "duplicate", "batch": n}
            if n != m.max_batch + 1:
                m.out_of_order += 1
                log.warning("묶음 번호가 이어지지 않음 meeting=%s 받은 번호=%s 기대 번호=%s", m.meeting_id, n, m.max_batch + 1)
                raise BatchOutOfOrder(m.max_batch + 1)
            m.max_batch = n
            if m.status == "new":
                m.status = "running"
            m.counts["batches"] += 1
            m.counts["lines"] += len(a["lines"])
            m.counts["edits"] += len(a["edits"])
            a["sent_ms"] = max((l["end_ms"] for l in a["lines"]), default=0)
            m.queue.put(("batch", a))
            self._ensure_worker(m)
            log.info("묶음 수신 meeting=%s batch=%s lines=%s edits=%s", m.meeting_id, n, len(a["lines"]), len(a["edits"]))
        return {"accepted": True, "result": "accepted", "batch": n}

    def receive_event(self, path_meeting_id, msg):
        self._require_ready()
        ev = adapt_event(msg)
        self._check_same_id(path_meeting_id, ev["meeting_id"])
        m = self._get(ev["meeting_id"], ev.get("meeting_id_raw"))
        name, at_ms = ev["event"], ev["at_ms"]
        with m.lock:
            if (name, at_ms) in m.seen_events:
                log.info("중복 알림 무시 meeting=%s event=%s", m.meeting_id, name)
                return {"accepted": True, "result": "duplicate", "event": name}
            warning = self._transition_warning(m, name)
            m.seen_events.add((name, at_ms))
            m.events.append({"event": name, "atMs": at_ms})
            if name in ("start", "resume"):
                m.status = "running"
            elif name == "pause":
                m.status = "paused"
            elif name == "end":
                m.status = "ended"
                m.queue.put(("end", ev))
                self._ensure_worker(m)
            if warning:
                m.warnings += 1
                log.warning("알림 순서가 이상함 meeting=%s event=%s: %s", m.meeting_id, name, warning)
            log.info("알림 수신 meeting=%s event=%s atMs=%s", m.meeting_id, name, at_ms)
        out = {"accepted": True, "result": "accepted", "event": name}
        if warning:
            out["warning"] = warning
        return out

    def receive_agendas(self, path_meeting_id, msg):
        """안건 목록 전체 (agenda-contract.md 4장). 받으면 바로 응답하고 분류는 뒤에서 한다."""
        self._require_ready()
        a = adapt_agendas(msg)
        self._check_same_id(path_meeting_id, a["meeting_id"])
        m = self._get(a["meeting_id"], a.get("meeting_id_raw"))
        with m.lock:
            if m.status == "ended":
                return {"accepted": False, "result": "ignored", "reason": "ended"}
        result = m.classifier.set_agendas(a["version"], a["agendas"])
        log.info("안건 목록 수신 meeting=%s version=%s agendas=%s result=%s", m.meeting_id, a["version"], len(a["agendas"]), result)
        if result == "stale":
            return {"accepted": True, "result": "stale"}
        m.queue.put(("tick", None))                             # 모아 둔 줄을 분류하고, 안건 삭제로 바뀐 줄을 내보낸다
        self._ensure_worker(m)
        return {"accepted": True, "result": "accepted", "version": a["version"]}

    def receive_corrections(self, path_meeting_id, msg):
        """사람이 고친 분류 (agenda-contract.md 7장)."""
        self._require_ready()
        c = adapt_corrections(msg)
        self._check_same_id(path_meeting_id, c["meeting_id"])
        m = self._get(c["meeting_id"], c.get("meeting_id_raw"))
        with m.lock:
            if m.status == "ended":
                return {"accepted": False, "result": "ignored", "reason": "ended"}
        applied = m.classifier.apply_corrections(c["corrections"])
        log.info("고친 분류 수신 meeting=%s corrections=%s applied=%s", m.meeting_id, len(c["corrections"]), applied)
        if applied:
            m.queue.put(("tick", None))
            self._ensure_worker(m)
        return {"accepted": True, "result": "accepted" if applied else "stale", "applied": applied}

    def receive_summary_edits(self, path_meeting_id, msg):
        """사람이 고친 요약 문장 (agenda-summary-contract.md 5장)."""
        self._require_ready()
        e = adapt_summary_edits(msg)
        self._check_same_id(path_meeting_id, e["meeting_id"])
        m = self._get(e["meeting_id"], e.get("meeting_id_raw"))
        with m.lock:
            if m.status == "ended":
                return {"accepted": False, "result": "ignored", "reason": "ended"}
        r = m.agenda_summarizer.apply_edits(e["edits"])
        if r["applied"]:
            m.queue.put(("tick", None))                         # 고친 안건은 기다리지 않고 다음 주기에 다시 요약한다
            self._ensure_worker(m)
        return {"accepted": True, "result": "accepted" if r["applied"] else "stale", "applied": r["applied"], "ignored": r["ignored"]}

    @staticmethod
    def _transition_warning(m, name):
        started = any(e["event"] == "start" for e in m.events)
        if name == "start" and started:
            return "start가 이미 있음"
        if name == "pause" and m.status != "running":
            return f"진행 중이 아닌데 pause (현재 {m.status})"
        if name == "resume" and m.status != "paused":
            return f"멈춘 상태가 아닌데 resume (현재 {m.status})"
        return None

    # ---------------- 뒤에서 처리 ----------------
    def _ensure_worker(self, m):
        if m.worker is None or not m.worker.is_alive():
            m.worker = threading.Thread(target=self._run, args=(m,), name=f"meeting-{m.meeting_id}", daemon=True)
            m.worker.start()

    def _run(self, m):
        while True:
            kind, payload = m.queue.get()
            try:
                # 요약과 안건 분류는 서로 실패해도 영향을 주지 않는다
                if kind == "batch":
                    self._step(m, "batch", lambda: (m.summarizer.receive(payload), self._maybe_publish(m, final=False)))
                    self._step(m, "classify", lambda: self._classify(m, batch=payload))
                    self._step(m, "agenda_status", lambda: m.status_judge.process())
                    self._step(m, "agenda_summary", lambda: self._agenda_summary(m))
                elif kind == "end":
                    def finish_summary():
                        m.summarizer.now_ms = max(m.summarizer.now_ms, payload["at_ms"])
                        m.summarizer.finish()
                        self._maybe_publish(m, final=True)
                    self._step(m, "end", finish_summary)
                    self._step(m, "classify", lambda: self._classify(m, end_ms=payload["at_ms"], final=True))
                    self._step(m, "agenda_status", lambda: m.status_judge.process(final=True))
                    self._step(m, "agenda_summary", lambda: self._agenda_summary(m, final=True))
                elif kind == "tick":
                    self._step(m, "classify", lambda: self._classify(m))
                    self._step(m, "agenda_status", lambda: m.status_judge.process())
                    self._step(m, "agenda_summary", lambda: self._agenda_summary(m))
            finally:
                m.queue.task_done()

    @staticmethod
    def _step(m, name, fn):
        try:
            fn()
        except Exception as e:                                  # 한 건이 실패해도 일꾼은 계속 돈다
            m.errors += 1
            log.error("처리 실패 meeting=%s kind=%s error=%s", m.meeting_id, name, type(e).__name__)

    def _classify(self, m, batch=None, end_ms=None, final=False):
        """줄 묶음을 분류기에 넘기고, 분류할 때가 되었으면 분류한 뒤 바뀐 것을 백엔드로 보낸다."""
        if batch is not None:
            m.classifier.receive(batch)
        if end_ms is not None:
            m.classifier.advance(end_ms)
        m.classifier.process(final=final)
        self._publish_classification(m, final)

    def _publish_classification(self, m, final):
        if self.publisher is None or not self.publisher.enabled:
            return
        r = m.classifier.take_result(final=final)
        if r is None:
            return
        m.cls_version = max(m.cls_version + 1, int(time.time() * 1000))     # 재시작해도 줄어들지 않도록 시각(밀리초) 이상으로
        payload = {"schemaVersion": r["schemaVersion"], "meetingId": m.id_raw, "version": m.cls_version,
                   **{k: v for k, v in r.items() if k not in ("schemaVersion", "meetingId")}}
        self.publisher.submit(m.meeting_id, payload, kind=AGENDA_CLASSIFICATION)

    def _agenda_summary(self, m, final=False):
        """분류를 마친 직후 안건별 요약을 갱신하고, 바뀐 안건 보드 전체 상태를 백엔드로 보낸다."""
        m.agenda_summarizer.process(final=final)
        if self.publisher is None or not self.publisher.enabled:
            return
        board = m.agenda_summarizer.take_board(status_info=m.status_judge.board_status())
        if board is not None:
            self.publisher.submit(m.meeting_id, board, kind=AGENDA_BOARD)

    def _maybe_publish(self, m, final):
        """요약이 바뀌었으면(또는 종료이면) 백엔드로 보낼 목록에 올린다."""
        if self.publisher is None or not self.publisher.enabled:
            return
        summary = m.summarizer.export_summary()["summary"]
        sig = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        if not final and (sig == m.last_sig or (not summary and m.last_sig is None)):
            return
        m.last_sig = sig
        m.version = max(m.version + 1, int(time.time() * 1000))   # 재시작해도 줄어들지 않도록 시각(밀리초) 이상으로
        self.publisher.submit(m.meeting_id, {
            "schemaVersion": "0.1",
            "meetingId": m.id_raw,
            "version": m.version,
            "atMs": m.summarizer.now_ms,
            "final": final,
            "summary": summary,
        })

    def wait_idle(self, meeting_id, timeout=10.0):
        """시험용: 대기열이 빌 때까지 기다린다."""
        m = self.meetings.get(meeting_id)
        if m is None:
            return True
        done = threading.Event()

        def waiter():
            m.queue.join()
            done.set()
        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)

    # ---------------- 상태 (전사 내용은 넣지 않는다) ----------------
    def status(self, meeting_id):
        m = self.meetings.get(meeting_id)
        if m is None:
            return None
        with m.lock:
            return {
                "meetingId": m.meeting_id,
                "status": m.status,
                "lastBatch": m.max_batch,
                "received": dict(m.counts),
                "outOfOrder": m.out_of_order,
                "events": list(m.events),
                "warnings": m.warnings,
                "errors": m.errors,
                "queued": m.queue.qsize(),
                "llmCalls": m.summarizer.calls,
                "provider": self.cfg.provider,
                "summaryVersion": m.version,
                "agendaClassification": {**m.classifier.info(), "version": m.cls_version},
                "agendaSummary": m.agenda_summarizer.info(),
                "agendaStatus": m.status_judge.info(),
                "publisher": self.publisher.info() if self.publisher else {"enabled": False},
            }
