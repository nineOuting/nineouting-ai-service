#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
수신 서비스: 회의별 상태를 들고, 백엔드에서 온 회의 알림과 줄 묶음을 요약 코드에 넘긴다.

FastAPI 없이 표준 라이브러리만 쓴다 (server/main.py 가 이 클래스를 HTTP로 감싼다).

흐름
  받기(바로 응답) : 형식 검사 -> 중복·순서 확인 -> 회의별 대기열에 넣기
  처리(뒤에서)    : 회의마다 일꾼 스레드 하나가 대기열을 순서대로 꺼내 요약 코드에 넘김

로그 규칙: 전사 내용은 로그에 남기지 않는다. 회의 번호, 묶음 번호, 개수, 예외 이름만 남긴다.
"""
import logging
import os
import queue
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY_DIR = os.path.abspath(os.path.join(HERE, "..", "summary"))
if SUMMARY_DIR not in sys.path:
    sys.path.insert(0, SUMMARY_DIR)

from input_adapter import adapt_batch, adapt_event, AdapterError  # noqa: E402
from meeting_live_summary_v2 import LiveSummarizer, Cfg  # noqa: E402

log = logging.getLogger("ai-service")

SUPPORTED_PROVIDERS = ("echo", "gemini", "anthropic", "openai")   # bedrock 은 다음 작업에서 추가


class ConfigError(ValueError):
    """환경변수 설정이 잘못됐을 때."""


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


class MeetingState:
    def __init__(self, meeting_id, cfg, llm=None):
        self.meeting_id = meeting_id
        self.summarizer = LiveSummarizer(meeting_id, cfg, llm=llm, verbose=False)
        self.status = "new"                 # new -> running <-> paused -> ended
        self.lock = threading.Lock()
        self.queue = queue.Queue()
        self.worker = None
        self.seen_batches = set()
        self.max_batch = 0
        self.gaps = []                      # 건너뛴 묶음 번호 구간 [(처음, 끝), ...]
        self.seen_events = set()            # (event, atMs)
        self.events = []                    # [{"event", "atMs"}]
        self.warnings = 0
        self.counts = {"batches": 0, "lines": 0, "edits": 0}
        self.errors = 0


class Registry:
    def __init__(self, cfg, llm=None):
        self.cfg = cfg
        self.llm = llm                      # 시험용: 모델 호출 대신 쓸 함수
        self.meetings = {}
        self.lock = threading.Lock()

    # ---------------- 받기 ----------------
    def _get(self, meeting_id):
        with self.lock:
            m = self.meetings.get(meeting_id)
            if m is None:
                m = self.meetings[meeting_id] = MeetingState(meeting_id, self.cfg, self.llm)
            return m

    @staticmethod
    def _check_same_id(path_id, body_id):
        if path_id != body_id:
            raise AdapterError(f"meetingId가 주소와 다름 (주소 {path_id}, 본문 {body_id})")

    def receive_batch(self, path_meeting_id, msg):
        a = adapt_batch(msg)                                   # 형식이 틀리면 AdapterError
        self._check_same_id(path_meeting_id, a["meeting_id"])
        m = self._get(a["meeting_id"])
        n = a["seq"]
        with m.lock:
            if m.status == "ended":
                log.warning("종료된 회의의 묶음 무시 meeting=%s batch=%s", m.meeting_id, n)
                return {"accepted": False, "result": "ignored", "reason": "ended", "batch": n}
            if n in m.seen_batches:
                log.info("중복 묶음 무시 meeting=%s batch=%s", m.meeting_id, n)
                return {"accepted": True, "result": "duplicate", "batch": n}
            gap = None
            if n > m.max_batch + 1:
                gap = (m.max_batch + 1, n - 1)
                m.gaps.append(gap)
                log.warning("묶음 번호가 건너뜀 meeting=%s 빠진 번호=%s~%s", m.meeting_id, gap[0], gap[1])
            m.seen_batches.add(n)
            m.max_batch = max(m.max_batch, n)
            if m.status == "new":
                m.status = "running"
            m.counts["batches"] += 1
            m.counts["lines"] += len(a["lines"])
            m.counts["edits"] += len(a["edits"])
            a["sent_ms"] = max((l["end_ms"] for l in a["lines"]), default=0)
            m.queue.put(("batch", a))
            self._ensure_worker(m)
            log.info("묶음 수신 meeting=%s batch=%s lines=%s edits=%s", m.meeting_id, n, len(a["lines"]), len(a["edits"]))
        out = {"accepted": True, "result": "accepted", "batch": n}
        if gap:
            out["gap"] = {"from": gap[0], "to": gap[1]}
        return out

    def receive_event(self, path_meeting_id, msg):
        ev = adapt_event(msg)
        self._check_same_id(path_meeting_id, ev["meeting_id"])
        m = self._get(ev["meeting_id"])
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
                if kind == "batch":
                    m.summarizer.receive(payload)
                elif kind == "end":
                    m.summarizer.now_ms = max(m.summarizer.now_ms, payload["at_ms"])
                    m.summarizer.finish()
            except Exception as e:                              # 한 건이 실패해도 일꾼은 계속 돈다
                m.errors += 1
                log.error("처리 실패 meeting=%s kind=%s error=%s", m.meeting_id, kind, type(e).__name__)
            finally:
                m.queue.task_done()

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
                "gaps": [{"from": a, "to": b} for a, b in m.gaps],
                "events": list(m.events),
                "warnings": m.warnings,
                "errors": m.errors,
                "queued": m.queue.qsize(),
                "llmCalls": m.summarizer.calls,
                "provider": self.cfg.provider,
            }
