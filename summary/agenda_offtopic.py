#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주제 변경 감지·새 안건 제안 (AI-06)

등록된 안건과 관계없는 이야기("해당 없음")가 오래 이어지면 새 안건 등록을 제안하고, 제안을 무시한 뒤에도 이어지면
"지금 다른 이야기를 하고 있다"고 알린다. 형식은 docs/api/agenda-offtopic-contract.md, 설계는 ai06-design-decisions-v1.md.

감지 (모델 없이 분류 결과만 읽는다)
  - 분류 결과가 해당 없음인 줄이 끊김 없이 이어진 가장 최근 구간(run)을 본다. 길이는 구간 첫 줄의 시작부터 마지막 줄의 끝까지(음성 시간)이다.
  - 구간 중간에 안건 줄이 하나라도 분류되면 처음부터 다시 센다 (엄격한 연속 판정).
  - 맞장구, 간투사, 낮은 확신도 줄은 건너뛴다: 구간을 끊지도, 길이를 늘리지도 않는다. 아직 분류하지 못한 줄도 건너뛴다.
  - 사람이 고친 분류가 있으면 고친 값으로 본다 (분류기의 규칙과 같다).
  - 매번 분류기 상태에서 다시 계산한다. 따로 들고 있는 것은 아래 다섯 가지뿐이다: 구간(episode) 기록, 제목, 무시 기록, 마지막 rev, 안건 key 목록.

단계 (보드의 offTopic.stage)
  SUGGEST  구간이 threshold_sec(기본 60초)에 닿았다 -> 새 안건 등록 제안.
  LONG     제안을 무시한 시각부터 해당 없음이 long_sec(기본 180초) 더 이어졌다 -> "지금 다른 이야기를 하고 있다" 알림.
  (없음)   구간이 짧다 / 무시한 뒤 기다리는 중 / LONG 을 무시했다 / 새 안건이 등록됐다 / 회의가 끝났다 -> offTopic 은 null.
  구간이 끊기면(안건 줄이 분류되면) 구간 기록과 제목을 지우고, 다음 구간이 다시 threshold_sec 에 닿으면 SUGGEST 부터 다시 시작한다.

무시 기록: dismiss(rev, at_ms). at_ms 가 없으면 받은 시점의 음성 시계(분류기의 now_ms)를 쓰고, 지금 제안이 떠 있지 않으면 받지 않는다.
  at_ms 가 있으면(재시작 복구) 그대로 보관만 한다. 현재 구간의 시작 + threshold 보다 이른 무시는 지난 구간의 것이라 쓰지 않는다.
  LONG 이 떠 있는 동안 받은 무시는 그 구간이 끝날 때까지 알림을 숨긴다 (구간당 한 번만 알린다).

새 안건 등록: 안건 목록에 처음 보는 key 가 생기면 떠 있던 제안을 닫는다 (구간이 끊길 때까지 다시 띄우지 않는다). 이전 줄을 새 안건으로 옮기지는 않는다.

제목: 구간이 처음 SUGGEST/LONG 이 될 때 모델을 한 번 불러 새 안건 제목을 만든다 (task="agenda_offtopic"). 구간 줄(최근 max_lines 개)과 등록된 안건 제목을 주고,
  제목과 근거 줄을 받는다. 근거는 meeting_summary.validate_items 로 확인하고, 제목이 길거나 등록된 안건과 같으면 오류를 알려 1회 더 부른다.
  끝내 통과하지 못하거나 호출이 실패해도 제목만 null 로 두고 알림은 그대로 낸다. 구간이 이어지는 동안 제목은 다시 만들지 않는다.
  호출이 연속 3번 실패하거나 호출 상한에 닿으면 제목 만들기만 멈춘다 (감지는 계속한다).

스레드: 일꾼 스레드(process, board)와 요청 스레드(dismiss, info)가 함께 쓴다. 상태 변경은 락 안에서 하고 모델 호출은 락 밖에서 한다.
        분류기의 락은 읽을 때만 잠깐 잡고(_view), 이 객체의 락과 겹쳐 잡지 않는다.

로그 규칙: 전사 내용, 안건 제목, 제안 제목을 남기지 않는다. 개수와 예외 이름만 남긴다.
LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import validate_items, call_llm, parse_llm_json, mmss, squash  # noqa: E402
from agenda_classifier import _clean  # noqa: E402

log = logging.getLogger("ai-service.agenda-offtopic")

TASK = "agenda_offtopic"              # 토큰 사용량 지표의 ai.task 값
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 제목 만들기를 멈춘다
MAX_DISMISSALS = 20                   # 보관하는 무시 기록 수

SUGGEST = "SUGGEST"
LONG = "LONG"
STAGES = (SUGGEST, LONG)

SYSTEM_PROMPT = """너는 회의에서 등록된 안건과 관계없이 이어지는 이야기를 보고, 새 안건으로 등록할 제목을 제안하는 도구야. 입력은 STT가 만든 전사 줄이야.

규칙
1. [등록된 안건]은 이미 있는 안건이야. 이 안건과 같거나 겹치는 제목은 만들지 마.
2. [전사]는 등록된 안건과 관계없다고 분류된 줄이야. 이 줄들이 공통으로 다루는 주제를 짧은 명사구 하나로 제목을 만들어. 공백을 포함해 30자 이내로 써.
3. 전사에 없는 내용, 사람 이름, 숫자를 지어내지 마. 제목에 쓰는 말은 전사에서 가져와.
4. 제목의 근거로 삼은 줄 번호를 evidence에 [[시작, 끝], ...] 형태로 적어. 근거는 1개 이상이어야 해.
5. 줄들이 여러 주제로 흩어져 있으면 가장 마지막에 이어진 주제로 제목을 만들어. 어떤 주제로도 제목을 만들 수 없을 때만 title을 null로 하고 evidence는 빈 배열로 해.

출력은 JSON만. {"title": "제목 또는 null", "evidence": [[1, 3]]}"""


class OffTopicCfg:
    """주제 변경 감지 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.threshold_sec = 60           # 해당 없음이 이만큼 이어지면 새 안건 제안 (명세서 REQ-091 1분, 2026-10-28 변경. 이전 값은 PRD 3분)
        self.long_sec = 180               # 제안을 무시한 뒤 이만큼 더 이어지면 "지금 다른 이야기" 알림 (REQ-081)
        self.max_calls = 20               # 회의당 제목 호출 상한
        self.max_lines = 40               # 제목을 만들 때 모델에 주는 줄 수 (구간이 길면 가장 최근 줄)
        self.title_max_chars = 30         # 제안 제목의 최대 글자 수
        self.tail_lines = 12              # 긴 구간에서 제목을 못 만들었을 때 한 번 더 시도하는 최근 줄 수 (0이면 쓰지 않음)
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def echo_llm(user):
    """모델을 부르지 않는 모의 응답. 구조(제목, 근거)만 맞춘다."""
    return json.dumps({"title": "기타 논의", "evidence": [[1, 1]]}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, SYSTEM_PROMPT, user, task=TASK)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AgendaOffTopic:
    def __init__(self, classifier, cfg=None, llm=None, now=time.time):
        self.c = classifier
        self.cfg = cfg or OffTopicCfg()
        self.llm = llm or self._make_llm()
        self.now = now
        self.lock = threading.RLock()
        self.ep = None                # 지금 진행 중인 구간 기록 (없으면 None)
        self.ep_serial = 0
        self.dismissals = []          # 무시한 시각(음성 시간, ms) 목록
        self.last_rev = 0
        self.seen_keys = None         # 지난번에 본 안건 key 들 (새 안건 등록을 알아내는 데 쓴다)
        self.stage = None             # 지금 보드에 내는 단계 (None 이면 offTopic 은 null)
        self.ended = False
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.rejected = 0             # 검증을 통과하지 못해 제목을 내지 않은 횟수
        self.suggested = 0            # 제안(SUGGEST)을 낸 구간 수
        self.halted = None            # 제목 만들기를 멈춘 이유 (None이면 정상)

    def _make_llm(self):
        return make_llm(self.cfg)

    # ---------------- 분류기에서 읽기 ----------------
    def _view(self):
        """분류기의 지금 상태를 복사해 온다 (분류기 락은 여기서만 잠깐 잡는다)."""
        c = self.c
        with c.lock:
            snap = [dict(ln) for ln in c.store.snapshot()]
            keys = set(c.keys)
            classified = {ln["id"] for ln in snap if ln["id"] in c.locks or ln["id"] in c.labels}
            eff = {i: c._eff(i) for i in classified}
            titles = [a["title"] for a in c.agendas]
            now_ms = c.now_ms
        return {"snap": snap, "keys": keys, "classified": classified, "eff": eff, "titles": titles, "now_ms": now_ms}

    @staticmethod
    def _trailing_run(view):
        """해당 없음 줄이 끊김 없이 이어진 가장 최근 구간. 없으면 None.
        {"start": 첫 줄의 시작 ms, "end": 마지막 줄의 끝 ms, "lines": [줄]}"""
        keys, eff, classified = view["keys"], view["eff"], view["classified"]
        run = None
        for ln in view["snap"]:
            if ln.get("low_confidence") or ln.get("kind") in ("filler", "backchannel"):
                continue                                         # 구간을 끊지도 늘리지도 않는다
            i = ln["id"]
            if i not in classified:
                continue                                         # 아직 분류하지 못한 줄
            k = eff[i]
            if k is not None and k in keys:
                run = None                                       # 안건 줄: 처음부터 다시 센다
                continue
            end = ln.get("end_ms") or ln["start_ms"]
            if run is None:
                run = {"start": ln["start_ms"], "end": end, "lines": [ln]}
            else:
                run["end"] = max(run["end"], end)
                run["lines"].append(ln)
        return run

    # ---------------- 단계 계산 ----------------
    def _stage_of(self, run):
        """구간과 무시 기록으로 단계를 정한다. 락 안에서 부른다. 반환: SUGGEST / LONG / None."""
        thr = self.cfg.threshold_sec * 1000
        if run is None or run["end"] - run["start"] < thr:
            return None
        valid = sorted(a for a in self.dismissals if a >= run["start"] + thr)
        if not valid:
            return SUGGEST
        d1 = valid[0]
        long_ms = self.cfg.long_sec * 1000
        if run["end"] - d1 < long_ms:
            return None                                          # 무시했고 아직 기다리는 중
        if any(a >= d1 + long_ms for a in valid[1:]):
            return None                                          # LONG 을 무시했다: 이 구간에서는 더 알리지 않는다
        return LONG

    def _evaluate(self, view):
        """상태를 갱신한다. 락 안에서 부른다. 반환: 구간(없으면 None)."""
        run = self._trailing_run(view)
        keys = view["keys"]
        if self.seen_keys is not None and (keys - self.seen_keys) and self.ep is not None:
            self.ep["closed"] = True                             # 새 안건이 등록됐다: 이 구간에서는 다시 제안하지 않는다
        self.seen_keys = set(keys)
        thr = self.cfg.threshold_sec * 1000
        if run is None or run["end"] - run["start"] < thr:
            self.ep = None                                       # 구간이 끊겼거나 짧다: 구간 기록과 제목을 지운다 (무시 기록은 두어도 지난 구간의 것은 쓰이지 않는다)
            self.stage = None
            return run
        if self.ep is not None and run["start"] >= self.ep["end"]:
            self.ep = None                                       # 지난번에 본 구간이 끝난 뒤 시작한 새 구간이다 (끊긴 것을 놓쳤어도 새로 센다)
        if self.ep is None:
            self.ep_serial += 1
            self.ep = {"id": self.ep_serial, "wall": _iso(self.now()), "ms": view["now_ms"], "closed": False, "end": run["end"],
                       "title": None, "evidence": [], "title_tried": False}
            self.suggested += 1
            log.info("새 안건 제안 구간 감지 meeting=%s lines=%s", self.c.meeting_id, len(run["lines"]))
        self.ep["end"] = run["end"]
        self.stage = None if self.ep["closed"] else self._stage_of(run)
        return run

    # ---------------- 처리 ----------------
    def process(self, final=False):
        """분류 결과를 읽어 단계를 정하고, 제목이 필요하면 모델을 부른다. 반환: 모델을 부른 횟수."""
        view = self._view()
        with self.lock:
            if final:
                self.ended = True
                self.ep, self.stage = None, None
                return 0
            if self.ended:
                return 0
            run = self._evaluate(view)
            job = self._title_job(run, view) if (self.stage in STAGES and not self.ep["title_tried"]) else None
        if job is None:
            return 0
        return self._run_title(job)

    def _title_job(self, run, view):
        """락 안에서 부른다. 제목을 만들 일감. 만들 수 없는 상태면 제목 없이 넘어가고 None."""
        ep = self.ep
        ep["title_tried"] = True                                 # 실패해도 같은 구간에서는 다시 부르지 않는다
        if self.halted:
            return None
        if self.calls >= self.cfg.max_calls:
            self.halted = "호출 상한"
            log.warning("호출 상한에 도달해 제목 만들기를 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
            return None
        lines = run["lines"][-self.cfg.max_lines:]
        titles = list(view["titles"])
        return {"ep": ep["id"], "lines": lines, "user": self._user_text(titles, lines), "titles": titles, "tail": False}

    @staticmethod
    def _user_text(titles, lines):
        return ("[등록된 안건]\n" + "\n".join(f"- {_clean(t)}" for t in titles) +
                "\n\n[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {ln['text_marked']}" for k, ln in enumerate(lines, 1)))

    def _check_title(self, d, job):
        """모델 출력을 검증한다. 반환: (제목 또는 None, 근거 seq 목록, 오류 목록)."""
        if not isinstance(d, dict):
            return None, [], ["최상위가 객체가 아님"]
        title = d.get("title")
        if title is None:
            return None, [], []                                  # 제목을 만들 수 없다고 답한 것: 오류가 아니다
        if not isinstance(title, str) or not title.strip():
            return None, [], ["title은 비어 있지 않은 문자열이나 null이어야 함"]
        title = " ".join(title.split())
        errs = []
        if len(title) > self.cfg.title_max_chars:
            errs.append(f"제목이 {len(title)}자로 너무 김 (최대 {self.cfg.title_max_chars}자)")
        if re.search(r"[\r\n]", d["title"]):
            errs.append("제목은 한 줄이어야 함")
        sq = squash(title)
        if any(squash(t) == sq for t in job["titles"]):
            errs.append("등록된 안건과 같은 제목임")
        res = validate_items([{"text": title, "topic": None, "evidence": d.get("evidence")}], job["lines"])
        _it, verrs, _warns, spans = res[0]
        errs += verrs
        if errs:
            return None, [], errs
        ids = sorted({job["lines"][k - 1]["id"] for a, b in spans for k in range(a, b + 1)})
        return title, ids, []

    def _run_title(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur_user = job["user"], job["user"]
        title, ids, rejected, calls, ok = None, [], False, 0, False
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    break
                self.calls += 1
            calls += 1
            try:
                raw = self.llm(cur_user)
            except Exception as e:                               # 키 오류, 네트워크 오류, 사용량 제한 등
                with self.lock:
                    self._fail(type(e).__name__)
                break
            try:
                d = parse_llm_json(raw)
            except Exception as e:
                if attempt == 2:
                    with self.lock:
                        self._fail("ParseError")
                    break
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\nJSON 해석 실패({type(e).__name__}). 형식에 맞게 JSON만 다시 출력해."
                continue
            t, e_ids, errs = self._check_title(d, job)
            if errs and attempt == 1:
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n" + "\n".join(f"- {m}" for m in errs) + "\n오류를 고쳐서 JSON을 다시 출력해."
                continue
            title, ids, rejected, ok = t, e_ids, bool(errs), True
            break
        with self.lock:
            if ok:
                self.err_streak = 0
            if rejected:
                self.rejected += 1
            ep = self.ep
            if ep is not None and ep["id"] == job["ep"]:
                ep["title"], ep["evidence"] = title, ids
        n = self.cfg.tail_lines
        if ok and title is None and not rejected and not job["tail"] and n and len(job["lines"]) > n and not self.halted:
            # 긴 구간이 여러 주제로 흩어져 모델이 제목을 못 만들었다: 가장 최근 줄만 보여 주고 한 번 더 부른다
            lines = job["lines"][-n:]
            job2 = {"ep": job["ep"], "lines": lines, "user": self._user_text(job["titles"], lines), "titles": job["titles"], "tail": True}
            calls += self._run_title(job2)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("제목 만들기를 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    # ---------------- 사람의 응답 ----------------
    def dismiss(self, rev, at_ms=None):
        """제안(또는 "지금 다른 이야기" 알림)을 무시했다는 알림. 반환: "applied" / "stale"(rev 가 오래됨) / "not_active"(떠 있는 제안이 없음)."""
        view = self._view()
        with self.lock:
            if rev <= self.last_rev:
                return "stale"
            if at_ms is None:
                run = self._evaluate(view)                       # 지금 상태를 최신으로 맞춘 뒤 제안이 떠 있는지 본다
                if self.stage is None:
                    return "not_active"
                at = view["now_ms"]
            else:
                at = at_ms                                       # 재시작 복구: 구간이 아직 다시 만들어지기 전일 수 있어 그대로 보관한다
            self.last_rev = rev
            if at not in self.dismissals:
                self.dismissals.append(at)
                self.dismissals = self.dismissals[-MAX_DISMISSALS:]
            log.info("제안 무시 meeting=%s rev=%s replay=%s", self.c.meeting_id, rev, at_ms is not None)
            return "applied"

    # ---------------- 출력 ----------------
    def board(self):
        """안건 보드의 offTopic 칸. 제안이 떠 있지 않으면 None."""
        with self.lock:
            if self.stage is None or self.ep is None:
                return None
            ep = self.ep
            return {"stage": self.stage, "detectedAt": ep["wall"], "detectedAtMs": ep["ms"],
                    "suggestedTitle": ep["title"], "evidenceSeqs": list(ep["evidence"])}

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 제안 제목은 넣지 않는다."""
        with self.lock:
            return {"stage": self.stage, "suggested": self.suggested, "dismissals": len(self.dismissals),
                    "calls": self.calls, "errors": self.errors, "rejected": self.rejected, "halted": self.halted}


# --------------------------------------------------------------------------------------
def selftest():
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    def ln(i, start, dur, kind=None, low=False, text=None):
        t = text or f"잡담 문장 {i}"
        return {"id": i, "start_ms": start, "end_ms": start + dur, "arrival_ms": start + dur, "type": "final",
                "kind": kind, "low_confidence": low, "text": t, "text_marked": t, "manual": False}

    def ag(i, title, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": f"{title} 결정", "added": False}

    A1, A2 = ag("a1", "배포 일정", 1), ag("a2", "예산 배분", 2)

    class World:
        """분류 결과를 직접 정해 주는 시험용 환경. 줄은 10초씩 이어 붙인다."""

        def __init__(self, llm=None, now_value=1000.0, default_cfg=False, **cfg_kw):
            if not default_cfg:
                cfg_kw.setdefault("threshold_sec", 180)       # 기존 시험은 180초 기준의 논리를 본다. 서비스 기본값(60초)은 default_cfg=True 로 따로 시험한다
            self.c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
            self.c.set_agendas(1, [A1, A2])
            self.t = 0
            self.n = 0
            self.prompts = []

            def spy(user):
                self.prompts.append(user)
                return (llm or echo_llm)(user)
            self.clock = [now_value]
            self.o = AgendaOffTopic(self.c, OffTopicCfg(**cfg_kw), llm=spy, now=lambda: self.clock[0])

        def add(self, label, dur=10000, kind=None, low=False, classify=True):
            """label: 안건 key, None(해당 없음). 줄 하나를 더한다."""
            self.n += 1
            self.c.receive({"lines": [ln(self.n, self.t, dur, kind, low)], "edits": [], "sent_ms": self.t + dur})
            if classify and kind not in ("filler", "backchannel") and not low:
                self.c.labels[self.n] = label
                self.c.pending.discard(self.n)
            self.t += dur
            return self.n

        def none(self, count, dur=10000):
            return [self.add(None, dur) for _ in range(count)]

        def go(self, final=False):
            self.o.process(final=final)
            return self.o.board()

    def stage(w):
        b = w.go()
        return b["stage"] if b else None

    # 1) 경계: 179초는 아니고 180초는 제안
    w = World()
    w.none(17)
    w.add(None, 9000)
    assert stage(w) is None, "179초에 제안이 떴다"
    w2 = World()
    w2.none(18)
    b = w2.go()
    assert b and b["stage"] == SUGGEST, b
    assert b["suggestedTitle"] == "기타 논의" and b["evidenceSeqs"] == [1] and b["detectedAt"].endswith("Z") and b["detectedAtMs"] > 0, b
    assert w2.o.calls == 1 and w2.o.info()["suggested"] == 1

    # 1-1) 기본 설정: 제안은 60초(명세서 REQ-091), 재알림은 무시한 뒤 180초(REQ-092). 서비스 기본값 그대로 경계를 본다
    assert OffTopicCfg().threshold_sec == 60 and OffTopicCfg().long_sec == 180
    w = World(default_cfg=True)
    w.none(5)
    w.add(None, 9000)                                             # 59초
    assert stage(w) is None, "59초에 제안이 떴다"
    w = World(default_cfg=True)
    w.none(5)                                                     # 50초
    assert stage(w) is None
    w.none(1)                                                     # 60초
    b = w.go()
    assert b and b["stage"] == SUGGEST and b["evidenceSeqs"] == [1] and w.o.calls == 1, b
    w.none(12)                                                    # 구간이 180초가 돼도 제안은 그대로이고 제목을 다시 만들지 않는다
    assert stage(w) == SUGGEST and w.o.calls == 1
    assert w.o.dismiss(1) == "applied" and stage(w) is None
    w.none(17)                                                    # 무시한 뒤 170초: 재알림은 아직이다
    assert stage(w) is None
    w.none(1)                                                     # 무시한 뒤 180초
    b = w.go()
    assert b and b["stage"] == LONG and w.o.calls == 1, b

    # 2) 엄격한 연속: 구간 사이에 안건 줄이 끼면 처음부터 다시 센다
    w = World()
    w.none(10)
    w.add("a1")
    w.none(10)
    assert stage(w) is None
    w.none(8)                                                     # 안건 줄 뒤로 180초가 쌓이면 다시 제안
    assert stage(w) == SUGGEST

    # 3) 맞장구·간투사·낮은 확신도 줄은 구간을 끊지도 늘리지도 않는다
    w = World()
    w.none(9)
    w.add(None, kind="filler")
    w.add(None, low=True)
    w.add(None, kind="backchannel")
    w.none(9)                                                     # 건너뛴 줄 사이의 시간도 구간에 든다: 첫 줄 시작 ~ 마지막 줄 끝 = 210초
    assert stage(w) == SUGGEST
    w = World()
    w.none(17)                                                    # 170초
    for _ in range(5):
        w.add(None, kind="backchannel")                           # 뒤에 맞장구가 이어져도 길이는 늘지 않는다
    w.add(None, low=True)
    w.add(None, kind="filler")
    assert stage(w) is None

    # 3-1) 맞장구·간투사·낮은 확신도 줄은 분류 결과가 있어도 세지 않는다 (사람이 고친 분류가 붙을 수 있다)
    for kind, low in (("filler", False), ("backchannel", False), (None, True)):
        w = World()
        w.none(17)                                                # 170초
        i = w.add(None, dur=20000, kind=kind, low=low)
        w.c.labels[i] = None
        w.c.pending.discard(i)
        assert stage(w) is None, f"건너뛸 줄을 구간에 셌다: {kind} {low}"
        w.add("a1", kind=kind, low=low)                           # 건너뛴 줄이 안건으로 분류돼 있어도 구간을 끊지 않는다
        w.c.labels[w.n] = "a1"
        w.none(2)
        assert stage(w) == SUGGEST, f"건너뛸 줄이 구간을 끊었다: {kind} {low}"

    # 4) 아직 분류하지 못한 줄은 건너뛴다
    w = World()
    w.none(9)
    w.add(None, classify=False)
    w.none(9)
    assert stage(w) == SUGGEST
    w = World()
    w.none(17)
    for _ in range(4):
        w.add(None, classify=False)
    assert stage(w) is None

    # 5) 사람이 고친 분류: 안건으로 고치면 끊기고, 해당 없음으로 고치면 이어진다
    w = World()
    ids = w.none(18)
    assert stage(w) == SUGGEST
    w.c.locks[ids[5]] = {"key": "a1", "rev": 1, "n": 1}
    assert stage(w) is None, "사람이 안건으로 고쳤는데 구간이 이어졌다"
    w = World()
    ids = w.none(9)
    mid = w.add("a2")
    w.none(8)
    assert stage(w) is None
    w.c.locks[mid] = {"key": None, "rev": 1, "n": 1}
    assert stage(w) == SUGGEST

    # 6) 제목은 한 번만 만든다. 구간이 끊긴 뒤 새 구간은 새로 만든다
    w = World()
    w.none(18)
    w.go(), w.go(), w.go()
    assert w.o.calls == 1, w.o.calls
    w.none(6)
    w.go()
    assert w.o.calls == 1
    w.add("a1")
    assert stage(w) is None and w.o.board() is None
    w.none(18)
    assert stage(w) == SUGGEST and w.o.calls == 2 and w.o.info()["suggested"] == 2
    assert "[등록된 안건]" in w.prompts[0] and "배포 일정" in w.prompts[0] and "[1] 00:00" in w.prompts[0]

    # 7) 무시 -> 3분 기다림 -> LONG -> 다시 무시하면 이 구간에서는 더 알리지 않는다
    w = World()
    w.none(18)
    assert stage(w) == SUGGEST
    assert w.o.dismiss(1) == "applied"
    assert stage(w) is None
    w.none(17)                                                    # 무시한 뒤 170초
    assert stage(w) is None
    w.none(1)                                                     # 180초
    b = w.go()
    assert b and b["stage"] == LONG and b["suggestedTitle"] == "기타 논의", b
    assert b["detectedAtMs"] == w.o.ep["ms"]
    assert w.o.calls == 1, "LONG 에서 제목을 다시 만들었다"
    assert w.o.dismiss(2) == "applied"
    assert stage(w) is None
    w.none(30)
    assert stage(w) is None, "LONG 을 무시했는데 다시 떴다"
    w.add("a2")
    w.none(18)
    assert stage(w) == SUGGEST, "새 구간은 무시 기록과 상관없이 다시 제안해야 한다"

    # 8) 무시 요청의 처리: 오래된 rev, 제안이 없을 때, 같은 rev 다시
    w = World()
    w.none(5)
    assert w.o.dismiss(1) == "not_active"
    assert w.o.dismissals == [] and w.o.last_rev == 0
    w.none(13)
    assert stage(w) == SUGGEST
    assert w.o.dismiss(5) == "applied"
    assert w.o.dismiss(5) == "stale" and w.o.dismiss(3) == "stale"
    assert len(w.o.dismissals) == 1

    # 9) 재시작 복구: 구간이 다시 만들어지기 전에 무시 기록(atMs)이 먼저 와도 반영한다. 지난 구간의 무시는 쓰지 않는다
    w = World()
    w.none(18)
    first_end = w.t
    w2 = World()
    assert w2.o.dismiss(1, at_ms=first_end) == "applied"          # 줄이 오기 전에 먼저 도착
    w2.go()                                                       # 줄이 오기 전에 한 번 처리해도 무시 기록은 남는다
    w2.none(18)
    assert stage(w2) is None, "복구한 무시 기록이 반영되지 않았다"
    w3 = World()
    w3.none(18)
    assert w3.o.dismiss(1, at_ms=500) == "applied"                # 구간 시작 + 180초보다 이른 시각: 이 구간의 무시가 아니다
    assert stage(w3) == SUGGEST

    # 10) 새 안건이 등록되면 떠 있던 제안을 닫고, 구간이 끊길 때까지 다시 띄우지 않는다
    w = World()
    w.none(18)
    assert stage(w) == SUGGEST
    w.c.set_agendas(2, [A1, A2, ag("a3", "채용 계획", 3)])
    assert stage(w) is None
    w.none(20)
    assert stage(w) is None
    w.add("a3")
    w.none(18)
    assert stage(w) == SUGGEST

    # 11) 회의가 끝나면 offTopic 은 null 이고 더 부르지 않는다
    w = World()
    w.none(18)
    assert stage(w) == SUGGEST
    assert w.go(final=True) is None
    w.none(30)
    assert w.go() is None and w.o.calls == 1

    # 12) 제목 만들기 실패: 알림은 제목 없이 나가고, 연속 3번 실패하면 제목 만들기만 멈춘다
    def boom(user):
        raise RuntimeError("키 오류")
    w = World(llm=boom)
    w.none(18)
    b = w.go()
    assert b and b["stage"] == SUGGEST and b["suggestedTitle"] is None and b["evidenceSeqs"] == [], b
    assert w.o.errors == 1 and w.o.calls == 1 and not w.o.halted
    for _ in range(2):
        w.add("a1")
        w.none(18)
        assert stage(w) == SUGGEST
    assert w.o.halted == "연속 실패" and w.o.calls == 3
    w.add("a1")
    w.none(18)
    assert stage(w) == SUGGEST and w.o.calls == 3, "멈춘 뒤에도 모델을 불렀다"

    # 13) 호출 상한
    w = World(max_calls=1)
    w.none(18)
    w.go()
    w.add("a1")
    w.none(18)
    b = w.go()
    assert b and b["suggestedTitle"] is None and w.o.calls == 1 and w.o.halted == "호출 상한"

    # 14) 검증: 긴 제목, 등록된 안건과 같은 제목, 범위 밖 근거는 오류를 알려 한 번 더 부르고, 그래도 안 되면 제목 없이 낸다.
    def bad_then_good(user):
        bad_then_good.n += 1
        if "[검증 오류]" in user:
            bad_then_good.saw_error = True
            return json.dumps({"title": "점심 메뉴", "evidence": [[1, 2]]}, ensure_ascii=False)
        return json.dumps({"title": "아주 긴 제목" * 10, "evidence": [[1, 1]]}, ensure_ascii=False)
    bad_then_good.n, bad_then_good.saw_error = 0, False
    w = World(llm=bad_then_good)
    w.none(18)
    b = w.go()
    assert bad_then_good.n == 2 and bad_then_good.saw_error and b["suggestedTitle"] == "점심 메뉴" and b["evidenceSeqs"] == [1, 2], b
    assert w.o.rejected == 0 and w.o.err_streak == 0
    for bad in ({"title": "배포  일정", "evidence": [[1, 1]]}, {"title": "점심", "evidence": [[1, 99]]},
                {"title": "점심", "evidence": []}, {"title": "", "evidence": [[1, 1]]}, {"title": 7, "evidence": [[1, 1]]}):
        w = World(llm=lambda u, bad=bad: json.dumps(bad, ensure_ascii=False))
        w.none(18)
        b = w.go()
        assert b and b["stage"] == SUGGEST and b["suggestedTitle"] is None, (bad, b)
        assert w.o.calls == 2 and w.o.rejected == 1, (bad, w.o.calls, w.o.rejected)
    w = World(llm=lambda u: json.dumps({"title": None, "evidence": []}))
    for _ in range(4):
        w.add(None, 50000)                                        # 줄 4개, 200초: 짧은 구간은 다시 부르지 않는다
    b = w.go()
    assert b["suggestedTitle"] is None and w.o.calls == 1 and w.o.rejected == 0, "모델이 제목을 못 만들겠다고 한 것은 거부가 아니다"
    w = World(llm=lambda u: json.dumps({"title": None, "evidence": []}))
    w.none(30)
    b = w.go()
    assert b["suggestedTitle"] is None and w.o.calls == 2 and w.o.rejected == 0, "긴 구간은 최근 줄로 한 번 더 부른다"
    assert len(w.prompts[1].split("[전사]\n")[1].splitlines()) == 12, "두 번째 호출은 최근 12줄만"

    def tail_only(u):
        rows = u.split("[전사]\n")[1].splitlines()
        if len(rows) > 12:
            return json.dumps({"title": None, "evidence": []})
        return json.dumps({"title": "최근 주제", "evidence": [[1, len(rows)]]}, ensure_ascii=False)
    w = World(llm=tail_only)
    w.none(30)
    b = w.go()
    assert b["suggestedTitle"] == "최근 주제" and w.o.calls == 2, b
    assert b["evidenceSeqs"] == list(range(19, 31)), "근거는 두 번째 호출에 준 최근 줄 번호"
    w = World(llm=tail_only, tail_lines=0)
    w.none(30)
    assert w.go()["suggestedTitle"] is None and w.o.calls == 1, "tail_lines=0 이면 쓰지 않는다"

    # 15) JSON 이 아닌 응답: 한 번 더 부르고, 또 아니면 실패로 센다
    w = World(llm=lambda u: "이건 JSON 이 아님")
    w.none(18)
    b = w.go()
    assert b["suggestedTitle"] is None and w.o.calls == 2 and w.o.errors == 1

    # 16) 구간이 길면 최근 줄만 모델에 준다
    w = World(max_lines=5)
    w.none(40)
    w.go()
    assert len(w.prompts[0].split("[전사]\n")[1].splitlines()) == 5

    # 17) 로그와 상태 조회에 전사·제목이 없다
    recs = []

    class H(logging.Handler):
        def emit(self, r):
            recs.append(r.getMessage())
    h = H()
    logging.getLogger("ai-service").addHandler(h)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)
    try:
        w = World()
        w.none(18)
        w.go()
        w.o.dismiss(1)
        w.none(20)
        w.go()
    finally:
        logging.getLogger("ai-service").removeHandler(h)
    blob = "\n".join(recs) + json.dumps(w.o.info(), ensure_ascii=False)
    assert recs and "잡담 문장" not in blob and "기타 논의" not in blob and "배포 일정" not in blob, blob

    # 17-1) 설정값이 실제로 쓰인다: 제안 시간과 재알림 시간을 따로 바꿀 수 있고, 감지 시각은 주입한 시계를 따른다
    w = World(threshold_sec=60, long_sec=30)
    w.none(5)
    w.add(None, 9000)
    assert stage(w) is None, "59초에 제안이 떴다"
    w = World(now_value=1000.0, threshold_sec=60, long_sec=30)
    w.none(6)
    b = w.go()
    assert b and b["stage"] == SUGGEST and b["detectedAt"] == "1970-01-01T00:16:40Z", b
    assert w.o.dismiss(1) == "applied"
    w.none(2)
    assert stage(w) is None
    w.none(1)
    assert stage(w) == LONG

    # 17-2) 제안 시간 전에 한 무시(재시작 복구 시각)는 쓰지 않는다: 제안 시간은 재알림 시간과 따로 읽는다
    w = World(threshold_sec=60, long_sec=30)
    w.none(7)                                                     # 70초
    start = w.o._trailing_run(w.o._view())["start"]
    assert w.o.dismiss(1, at_ms=start + 45000) == "applied"
    assert stage(w) == SUGGEST, "제안 시간 전의 무시가 쓰였다"

    # 18) 설정 오류
    try:
        OffTopicCfg(nope=1)
        raise SystemExit("알 수 없는 설정을 못 잡음")
    except TypeError:
        pass

    # 19) 처리와 요청이 동시에 들어와도 깨지지 않는다
    w = World()
    w.none(18)
    errs = []

    def hammer():
        try:
            for r in range(1, 200):
                w.o.dismiss(r)
                w.o.board()
        except Exception as e:                                   # pragma: no cover
            errs.append(e)
    th = threading.Thread(target=hammer)
    th.start()
    for _ in range(200):
        w.o.process()
    th.join()
    assert not errs, errs

    print("주제 변경 감지 자체 시험 통과 (경계 179/180초와 기본값 59/60초, 엄격한 연속, 맞장구·간투사·낮은 확신도, 미분류 줄, 고친 분류, 제목 1회, 무시 -> LONG -> 재무시, rev, 재시작 복구, 새 안건 등록, 종료, 실패·상한, 검증, 로그에 전사 없음, 동시 접근)")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
