#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건 상태 판정 (AI-04)

안건마다 상태 하나와 판정 근거를 만든다. 형식은 docs/api/agenda-status-contract.md, 예시는 examples/api/.

상태 (안건 보드 agendas[].status 값)
  NOT_STARTED    아직 안 다룸      연결된 줄이 없다 (모델 없이)
  DISCUSSING     논의 중           줄이 있고 결정이 없고, 지금 이 안건이 현재 안건이다 (모델 없이)
  UNDECIDED      미결              줄이 있고 결정이 없고, 다른 안건으로 넘어갔거나 회의가 끝났다 (모델 없이)
  DECIDED        결정              모델이 "결정할 사항에 대한 답이 나오고 합의 표현이 확인된다"고 판정하고 근거 줄이 검증을 통과했다
  DECISION_CHECK 결정 확인 필요    모델이 "답처럼 보이는 말은 있지만 확신이 낮다"고 판정하고 근거 줄이 검증을 통과했다 (사람이 확인한다)

동작
  - 모델은 줄이 있는 안건에 대해 "결정(decided) / 확인 필요(check) / 아직 결정 없음(none)" 셋 중 하나만 고른다.
    논의 중과 미결은 모델 없이 현재 안건(분류기의 규칙)으로 정한다.
  - 입력은 원문 줄뿐이다. 안건별 요약 문장은 쓰지 않는다 (요약 위에 쌓지 않는다).
    맞장구 줄은 합의 표현일 수 있어 넣는다. 맞장구는 분류 대상이 아니라 안건 표시가 없으므로, 바로 앞에 분류된 줄의 안건에
    붙인다(앞 줄의 끝에서 backchannel_gap_sec 이내이고, 그 사이에 해당 없음 줄이 없을 때). 간투사와 낮은 확신도 줄은 뺀다.
  - 결정과 확인 필요는 근거 줄이 필수다. 근거(evidence)와 이유(reason)는 meeting_summary.validate_items 로 확인하고,
    통과하지 못하면 오류를 알려 1회 더 부른다. 그래도 통과하지 못하면 판정하지 않고(none) 넘어간다. 근거 없는 결정은 내지 않는다.
  - 판정 시점: 줄이 새로 붙은 안건은 min_new_lines 개 이상이면 바로, 1개면 refresh_sec(음성 시간) 뒤. 줄이 빠져나갔거나 글이 바뀐
    안건은 기다리지 않는다. 회의가 끝나면(final) 바뀐 안건을 모두 한 번 더 판정하고, 결정이 아닌 안건은 미결로 확정한다.
  - 호출이 연속 3번 실패하거나 호출 상한에 닿으면 이 회의의 모델 판정만 멈춘다. 이미 내린 판정과 규칙 판정은 계속 낸다.
  - 사람이 고친 상태는 받지 않는다 (docs/api/agenda-status-contract.md 9장).

스레드: 일꾼 스레드(process, board_status)와 요청 스레드(info)가 함께 쓴다. 상태 변경은 락 안에서 하고 모델 호출은 락 밖에서 한다.
        분류기의 락은 읽을 때만 잠깐 잡고(_view), 판정기의 락과 겹쳐 잡지 않는다.

로그 규칙: 전사 내용, 안건 제목, 판정 이유를 남기지 않는다. 개수와 예외 이름만 남긴다.
모델 호출은 task="agenda_status" 로 토큰 지표에 남긴다. LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import hashlib
import json
import logging
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import validate_items, call_llm, parse_llm_json, mmss  # noqa: E402
from agenda_classifier import _clean  # noqa: E402

log = logging.getLogger("ai-service.agenda-status")

TASK = "agenda_status"                # 토큰 사용량 지표의 ai.task 값
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 이 회의의 모델 판정을 멈춘다

NOT_STARTED = "NOT_STARTED"
DISCUSSING = "DISCUSSING"
DECIDED = "DECIDED"
UNDECIDED = "UNDECIDED"
DECISION_CHECK = "DECISION_CHECK"
STATUSES = (NOT_STARTED, DISCUSSING, DECIDED, UNDECIDED, DECISION_CHECK)
VERDICTS = ("decided", "check", "none")

SYSTEM_PROMPT = """너는 회의에서 한 안건의 결정 여부를 판정하는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어. [맞장구]가 붙은 줄은 짧은 호응이야.

규칙
1. [안건]의 제목과 결정할 사항은 무엇을 결정해야 하는지 이해하는 데만 써. 전사에 없는 내용을 제목에서 가져오지 마.
2. verdict는 셋 중 하나야.
   - "decided": 결정할 사항에 대한 답이 전사에 나오고, 합의 표현(동의, 확정, 정리하는 말)도 전사에서 확인될 때.
   - "check": 결정할 사항에 대한 답처럼 보이는 말은 있지만, 합의가 분명하지 않거나 의견이 갈린 채 남았거나 STT가 불확실해서 확신하지 못할 때.
   - "none": 결정할 사항에 대한 답이 나오지 않았거나 아직 논의 중일 때.
3. 제안이나 의견만 나오고 아무도 동의하지 않았으면 decided가 아니야. check나 none으로 해.
4. 누가 말한 내용을 다른 사람이 받아서 확인하는 말("네", "좋아요", "그렇게 해요")은 합의 표현이 될 수 있어. 앞 줄과 함께 봐.
5. verdict가 decided나 check이면 reason에 한 문장으로 무엇이 결정됐는지(check이면 왜 확신하지 못하는지)를 쓰고, evidence에 근거 줄 번호 구간 [[첫 번호, 끝 번호], ...]을 달아. 구간의 양 끝은 입력에 있는 번호여야 해. 근거에는 답이 나온 줄과 합의 표현이 나온 줄을 모두 넣어.
6. reason에는 근거 줄에 없는 사실, 이름, 숫자를 쓰지 마. 말을 인용할 때는 「」로 묶고 입력 원문과 똑같이 써(⟨ ⟩ 기호는 빼). ⟨ ⟩로 묶인 글자는 STT가 확신하지 못한 부분이야. 그 부분을 근거로 쓰면 reason에 "STT 불확실"이라고 밝혀. 숫자는 말한 한글 표기를 「」로 그대로 인용해.
7. verdict가 none이면 reason은 빈 문자열, evidence는 빈 목록으로 줘.

출력은 JSON만. 형식:
{"verdict": "decided" 또는 "check" 또는 "none", "reason": "...", "evidence": [[첫 번호, 끝 번호]]}"""


class StatusCfg:
    """상태 판정 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.refresh_sec = 60         # 새 줄이 1개뿐일 때 기다리는 시간 (음성 시간 기준)
        self.min_new_lines = 2        # 새 줄이 이만큼이면 기다리지 않고 바로 판정
        self.max_calls = 300          # 회의당 모델 호출 상한 (비용을 추정한 뒤 정한다)
        self.max_lines = 200          # 한 번에 모델에 주는 줄 수 (넘으면 가장 최근 줄)
        self.backchannel_gap_sec = 10  # 맞장구를 앞 줄의 안건에 붙이는 최대 간격 (앞 줄의 끝부터, 음성 시간)
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


# --------------------------------------------------------------------------------------
# 모의 판정기 (모델을 부르지 않는다. 비용 0)
# --------------------------------------------------------------------------------------
def echo_llm(user):
    """마지막 줄의 말투로 정하는 모의 판정기. 구조(JSON 형식, 근거 번호)만 맞춘다.
    마지막 줄에 '확정'·'결정'·'하기로'가 있으면 decided, '글쎄'·'아마'가 있으면 check, 아니면 none."""
    body = user.split("[전사]\n", 1)[-1]
    rows = re.findall(r"^\[(\d+)\] (\d\d:\d\d) (.*)$", body, re.M)
    if not rows:
        return json.dumps({"verdict": "none", "reason": "", "evidence": []}, ensure_ascii=False)
    n, _t, text = rows[-1]
    text = re.sub(r"[⟨⟩]", "", text)
    if any(w in text for w in ("확정", "결정", "하기로")):
        v, why = "decided", "마지막 줄에 결정 표현이 나옴"
    elif any(w in text for w in ("글쎄", "아마")):
        v, why = "check", "마지막 줄에 불확실한 표현이 나옴"
    else:
        return json.dumps({"verdict": "none", "reason": "", "evidence": []}, ensure_ascii=False)
    return json.dumps({"verdict": v, "reason": why, "evidence": [[int(n), int(n)]]}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, SYSTEM_PROMPT, user, task=TASK)


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:10]


def _new_state():
    return {"verdict": None, "reason": None, "reflected": {}, "wait_since": None, "dirty": False}


def _parse_verdict(raw):
    """모델 응답 -> (verdict, reason, evidence). 형식이 틀리면 ValueError (메시지에는 입력 내용을 넣지 않는다)."""
    d = parse_llm_json(raw)
    verdict = d.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError("verdict 는 decided, check, none 중 하나여야 함")
    reason, evidence = d.get("reason"), d.get("evidence")
    if verdict != "none":
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("decided, check 에는 reason 이 필요함")
        if not isinstance(evidence, list):
            raise ValueError("evidence 가 목록이 아님")
    return verdict, (reason or ""), (evidence or [])


class AgendaStatusJudge:
    def __init__(self, classifier, cfg=None, llm=None):
        self.c = classifier
        self.cfg = cfg or StatusCfg()
        self.llm = llm or make_llm(self.cfg)
        self.lock = threading.RLock()
        self.ag = {}                  # 안건 key -> 상태 {"verdict", "reason", "reflected", "wait_since", "dirty"}
        self.known = set()            # 안건 목록에 한 번이라도 있었던 key (사라졌는지 가리는 데 쓴다)
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.rejected = 0             # 근거 검증을 통과하지 못해 판정하지 않고 넘어간 횟수
        self.halted = None            # 멈춘 이유 (None이면 정상)
        self.final = False            # 회의가 끝났다 (결정이 아닌 안건은 미결로 확정)

    # ---------------- 분류기에서 읽기 ----------------
    def _view(self):
        """분류기의 지금 상태를 복사해 온다 (분류기 락은 여기서만 잠깐 잡는다).
        rows: 안건 key -> [(줄, 맞장구인가)] 시간 순서. 맞장구는 바로 앞에 분류된 줄의 안건에 붙인다."""
        c = self.c
        gap = self.cfg.backchannel_gap_sec * 1000
        with c.lock:
            snap = [dict(ln) for ln in c.store.snapshot()]
            keys = set(c.keys)
            classified = {ln["id"] for ln in snap if ln["id"] in c.locks or ln["id"] in c.labels}
            eff = {i: c._eff(i) for i in classified}
            agendas = [dict(a) for a in c.agendas]
            current = c._current_key()
            ids = {a["key"]: c._id(a["key"]) for a in agendas}
            now_ms = c.now_ms
        rows, last_key, last_end = {}, None, None
        for ln in snap:
            if ln.get("low_confidence") or ln.get("kind") == "filler":
                continue
            if ln.get("kind") == "backchannel":
                if last_key is not None and ln["start_ms"] - last_end <= gap:
                    rows.setdefault(last_key, []).append((ln, True))
                continue
            i = ln["id"]
            if i not in classified:
                continue                                     # 아직 분류하지 못한 줄
            k = eff[i]
            if k is not None and k in keys:
                rows.setdefault(k, []).append((ln, False))
                last_key, last_end = k, (ln.get("end_ms") or ln["start_ms"])
            else:
                last_key = last_end = None                   # 해당 없음 줄이 끼면 그 뒤 맞장구는 붙이지 않는다
        return {"agendas": agendas, "keys": keys, "rows": rows, "current": current, "ids": ids, "now_ms": now_ms}

    # ---------------- 판정 ----------------
    def process(self, final=False, flush=False):
        """판정할 때가 된 안건을 판정한다. 반환: 모델을 부른 횟수.
        flush: 새 줄이 1개일 때 기다리는 규칙만 건너뛴다(종료 버튼을 눌렀을 때). 종료 확정(final)은 켜지 않는다."""
        view = self._view()
        with self.lock:
            if final:
                self.final = True
            if self.halted:
                return 0
            jobs = self._plan(view, final or flush)
        calls = 0
        for job in jobs:
            calls += self._run_job(job)
            if self.halted:
                break
        return calls

    def _plan(self, view, final):
        """락 안에서 부른다. 상태를 정리하고, 모델을 불러야 할 안건의 일감을 만든다."""
        cfg = self.cfg
        keys, now = view["keys"], view["now_ms"]
        for k in [k for k in self.ag if k in self.known and k not in keys]:
            del self.ag[k]                                   # 목록에서 사라진 안건
        self.known |= keys
        jobs = []
        for a in view["agendas"]:
            key = a["key"]
            st = self.ag.setdefault(key, _new_state())
            rows = view["rows"].get(key, [])
            if not rows:                                     # 줄이 없다: 판정을 비운다 (모델 없이 아직 안 다룸)
                st.update(verdict=None, reason=None, reflected={}, wait_since=None, dirty=False)
                continue
            cur = {ln["id"]: _h(ln["text"]) for ln, _bc in rows}
            refl = st["reflected"]
            lost = [i for i in refl if i not in cur]
            changed = [i for i in cur if i in refl and refl[i] != cur[i]]
            if lost or changed:
                st["dirty"] = True                           # 줄이 빠져나갔거나 글이 바뀜: 기다리지 않는다
            new_ids = [i for i in cur if i not in refl]
            if st["dirty"]:
                trigger = True
            elif new_ids:
                if final or len(new_ids) >= cfg.min_new_lines:
                    trigger = True
                else:
                    if st["wait_since"] is None:
                        st["wait_since"] = now
                    trigger = now - st["wait_since"] >= cfg.refresh_sec * 1000
            else:
                st["wait_since"] = None
                trigger = False
            if not trigger:
                continue
            jobs.append(self._make_job(a, rows, cur))
            st["dirty"] = False
        return jobs

    def _make_job(self, agenda, rows, cur):
        K = rows[-self.cfg.max_lines:]
        parts = [f"다음은 하나의 안건에 연결된 회의 발언이야 (줄 {len(K)}개). [맞장구]가 붙은 줄은 짧은 호응이야.",
                 f"[안건]\n제목: {_clean(agenda['title'])}\n결정할 사항: {_clean(agenda['decision_point'])}",
                 "[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {'[맞장구] ' if bc else ''}{ln['text_marked']}"
                                       for k, (ln, bc) in enumerate(K, 1))]
        return {"key": agenda["key"], "lines": [ln for ln, _bc in K], "reflected": cur, "user": "\n\n".join(parts)}

    def _run_job(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur_user = job["user"], job["user"]
        outcome, calls = None, 0              # outcome: (verdict 또는 "rejected", reason, 근거 줄 id 목록)
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    log.warning("상태 판정 호출 상한에 도달해 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
                    break
                self.calls += 1
            calls += 1
            try:
                raw = self.llm(cur_user)
            except Exception as e:                           # 키 오류, 네트워크 오류, 사용량 제한 등
                with self.lock:
                    self._fail(type(e).__name__)
                break
            try:
                verdict, reason, evidence = _parse_verdict(raw)
            except Exception as e:
                if attempt == 2:
                    with self.lock:
                        self._fail("ParseError")
                    break
                why = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else type(e).__name__
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\n형식 오류({why}). 형식에 맞게 JSON만 다시 출력해."
                continue
            if verdict == "none":
                outcome = ("none", None, [])
                break
            (_it, errs, _warns, spans), = validate_items([{"text": reason, "topic": None, "evidence": evidence}], job["lines"])
            if errs:
                if attempt == 1:
                    msg = "\n".join(f"- {e}" for e in errs)
                    cur_user = user + (f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg}\n"
                                       "오류를 고쳐서 JSON을 다시 출력해. 근거를 댈 수 없으면 verdict를 none으로 줘.")
                    continue
                outcome = ("rejected", None, [])             # 근거 없는 결정은 내지 않는다
                break
            ids = sorted({job["lines"][k - 1]["id"] for a, b in spans for k in range(a, b + 1)})
            outcome = (verdict, reason.strip(), ids)
            break
        with self.lock:
            st = self.ag.get(job["key"])
            if outcome is None:                              # 실패: 반영하지 않고 다음 주기에 다시 한다
                if st is not None:
                    st["dirty"] = True
                return calls
            self.err_streak = 0
            if st is not None:
                self._commit(job, st, outcome)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("상태 판정 호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("상태 판정을 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    def _commit(self, job, st, outcome):
        verdict, reason, ids = outcome
        if verdict == "rejected":
            self.rejected += 1
            log.info("근거 검증 탈락 meeting=%s rejected=%s", self.c.meeting_id, self.rejected)
        if verdict in ("decided", "check"):
            st["verdict"], st["reason"] = verdict, {"text": reason, "evidenceSeqs": ids}
        else:
            st["verdict"], st["reason"] = None, None
        st["reflected"], st["wait_since"] = job["reflected"], None
        log.info("안건 상태 판정 meeting=%s verdict=%s calls=%s", self.c.meeting_id, verdict, self.calls)

    # ---------------- 출력 ----------------
    def board_status(self, preview_final=False):
        """안건 key -> {"status", "reason": {"text", "evidenceSeqs"} 또는 None}. 안건 보드에 합쳐 보낸다.
        모델이 판정하지 않는 상태(아직 안 다룸, 논의 중, 미결)의 근거는 규칙으로 만든다.
        preview_final: 종료 확정을 가정한 값을 돌려준다(논의 중이던 안건은 미결). 판정기 상태는 바꾸지 않는다.
        종료 버튼을 눌렀을 때 미결 목록(AI-07)을 만드는 데 쓴다. 이어서 논의하기로 돌아가면 보드의 상태는 그대로 논의 중이다."""
        view = self._view()
        with self.lock:
            out = {}
            for a in view["agendas"]:
                key = a["key"]
                st = self.ag.get(key) or _new_state()
                rows = view["rows"].get(key, [])
                if not rows:
                    out[key] = {"status": NOT_STARTED, "reason": None}
                    continue
                n, last = len(rows), rows[-1][0]["id"]
                if st["verdict"] == "decided":
                    out[key] = {"status": DECIDED, "reason": dict(st["reason"])}
                elif st["verdict"] == "check":
                    out[key] = {"status": DECISION_CHECK, "reason": dict(st["reason"])}
                elif self.final:
                    out[key] = {"status": UNDECIDED, "reason": {
                        "text": f"회의가 끝날 때까지 결정 표현을 찾지 못함 (연결된 발언 {n}줄)", "evidenceSeqs": [last]}}
                elif view["current"] == key and preview_final:
                    out[key] = {"status": UNDECIDED, "reason": {
                        "text": f"종료를 요청한 시점까지 결정 표현을 찾지 못함 (연결된 발언 {n}줄)", "evidenceSeqs": [last]}}
                elif view["current"] == key:
                    out[key] = {"status": DISCUSSING, "reason": {
                        "text": f"연결된 발언 {n}줄이 이어지는 중", "evidenceSeqs": [last]}}
                else:
                    out[key] = {"status": UNDECIDED, "reason": {
                        "text": f"연결된 발언 {n}줄 뒤 다른 안건으로 넘어갔고 결정 표현을 찾지 못함", "evidenceSeqs": [last]}}
            return out

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 판정 이유는 넣지 않는다."""
        with self.lock:
            return {"agendas": len(self.ag),
                    "decided": sum(1 for s in self.ag.values() if s["verdict"] == "decided"),
                    "decisionCheck": sum(1 for s in self.ag.values() if s["verdict"] == "check"),
                    "calls": self.calls, "errors": self.errors, "rejected": self.rejected, "halted": self.halted}


# --------------------------------------------------------------------------------------
# 자체 시험 (모델을 부르지 않는다)
# --------------------------------------------------------------------------------------
def selftest():
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    def ln(i, start, text, kind=None, low=False, end=None):
        return {"id": i, "start_ms": start, "end_ms": end if end is not None else start + 1000, "arrival_ms": start + 1000,
                "type": "final", "kind": kind, "low_confidence": low, "text": text, "text_marked": text, "manual": False}

    def ag(i, title, decision, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": decision, "added": False}

    A1 = ag("a1", "배포 일정", "배포 날짜를 확정한다", 1)
    A2 = ag("a2", "예산 배분", "비용 한도를 정한다", 2)
    A3 = ag("a3", "채용 계획", "채용 인원을 정한다", 3)

    def fresh(agendas=(A1, A2), llm=None, **kw):
        c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
        if agendas:
            c.set_agendas(1, list(agendas))
        return c, AgendaStatusJudge(c, StatusCfg(**kw), llm=llm)

    def feed(c, lines, sent_ms):
        c.receive({"lines": lines, "edits": [], "sent_ms": sent_ms})
        c.process()

    def stat(j, k="a1"):
        return j.board_status()[k]

    records = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    h = Grab()
    logging.getLogger("ai-service").addHandler(h)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)

    def answer(verdict, reason="", ev=()):
        return json.dumps({"verdict": verdict, "reason": reason, "evidence": [list(e) for e in ev]}, ensure_ascii=False)

    # 1) 줄이 없으면 아직 안 다룸. 모델을 부르지 않는다
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"))
    assert j.process() == 0 and not seen
    bs = j.board_status()
    assert bs["a1"] == {"status": NOT_STARTED, "reason": None} and bs["a2"]["status"] == NOT_STARTED

    # 2) 줄이 새로 2개 이상이면 바로 판정. 결정 없음이면 현재 안건은 논의 중, 아니면 미결 (모델 없이)
    feed(c, [ln(1, 0, "배포는 언제 할까요"), ln(2, 4000, "배포 날짜 후보를 모아봐요"), ln(3, 8000, "비용 한도 얘기해요")], 9000)
    assert j.process() == 1 and len(seen) == 1, "새 줄이 2개인 안건 하나만 판정"
    assert "제목: 배포 일정" in seen[0] and "결정할 사항: 배포 날짜를 확정한다" in seen[0] and "[1] 00:00" in seen[0]
    assert stat(j, "a1")["status"] == UNDECIDED and stat(j, "a1")["reason"]["evidenceSeqs"] == [2], "현재 안건은 a2라 a1은 미결"
    assert stat(j, "a2")["status"] == DISCUSSING and stat(j, "a2")["reason"]["evidenceSeqs"] == [3], "줄 1개라 아직 판정 전, 현재 안건"
    assert j.process() == 0, "같은 내용은 다시 부르지 않음"

    # 3) 1줄은 refresh_sec 동안 기다린다 (음성 시간)
    n = j.calls
    c.advance(30000)
    assert j.process() == 0 and j.calls == n
    c.advance(80000)
    assert j.process() == 1, "60초가 지나면 판정"

    # 4) decided: 근거 줄이 통과하면 DECIDED, 근거는 줄 id
    answers = [answer("decided", "배포는 다음 주 목요일로 정함", [(1, 2)])]
    c, j = fresh(llm=lambda u: answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 4000, "배포 좋아요 그렇게 해요")], 5000)
    j.process()
    s = stat(j)
    assert s["status"] == DECIDED and s["reason"] == {"text": "배포는 다음 주 목요일로 정함", "evidenceSeqs": [1, 2]}, s
    # 5) check
    answers = [answer("check", "목요일이 맞는지 확신하기 어려움", [(2, 2)])]
    c, j = fresh(llm=lambda u: answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일 어때요"), ln(2, 4000, "배포 글쎄요 아마 괜찮을 듯")], 5000)
    j.process()
    assert stat(j)["status"] == DECISION_CHECK and stat(j)["reason"]["evidenceSeqs"] == [2]

    # 6) 근거 번호가 범위 밖이면 오류를 알려 한 번 더 부른다. 고치면 반영
    inputs = []
    answers = [answer("decided", "배포는 목요일", [(9, 9)]), answer("decided", "배포는 목요일", [(1, 2)])]
    c, j = fresh(llm=lambda u: inputs.append(u) or answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 동의해요")], 5000)
    assert j.process() == 2 and "[검증 오류]" in inputs[1] and "[이전 출력]" in inputs[1]
    assert stat(j)["status"] == DECIDED and j.rejected == 0
    # 7) 다시 불러도 근거가 틀리면 판정하지 않는다 (연속 실패로 세지 않음)
    answers = [answer("decided", "배포는 목요일", [(9, 9)])] * 2
    c, j = fresh(llm=lambda u: answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 동의해요")], 5000)
    j.process()
    assert stat(j)["status"] == DISCUSSING and j.rejected == 1 and j.err_streak == 0 and not j.halted
    # 8) 형식 오류(JSON 아님, verdict 없음)도 한 번 더, 둘 다 틀리면 실패
    answers = ["JSON 아님", answer("maybe")]
    c, j = fresh(llm=lambda u: answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 동의해요")], 5000)
    assert j.process() == 2 and j.errors == 1 and stat(j)["status"] == DISCUSSING

    # 9) 맞장구는 앞에 분류된 줄의 안건에 붙인다 (간격 이내). 간투사, 낮은 확신도 줄은 뺀다. 해당 없음 줄이 끼면 붙이지 않는다
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"))
    feed(c, [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 2500, "네 좋아요", kind="backchannel"),
             ln(3, 4000, "배포 일정 확정해요"), ln(4, 5500, "음", kind="filler"), ln(5, 6000, "배포 흐릿한 말", low=True),
             ln(6, 30000, "네네", kind="backchannel")], 31000)
    j.process()
    u = seen[-1]
    assert "[맞장구] 네 좋아요" in u and "음" not in u.split("[전사]")[1].replace("좋아요", "") and "흐릿한" not in u, u
    assert "네네" not in u, "간격이 길면 붙이지 않음"
    # 10) 줄이 빠지거나 글이 바뀌면 기다리지 않고 다시 판정, 결정/확인 필요도 새 줄이 오면 다시 판정
    answers = [answer("decided", "배포는 목요일", [(1, 2)]), answer("none")]
    c, j = fresh(llm=lambda u: answers.pop(0))
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 동의해요")], 5000)
    j.process()
    assert stat(j)["status"] == DECIDED
    feed(c, [ln(3, 8000, "배포 다시 생각해봐요"), ln(4, 9000, "배포 이전 결정은 보류해요")], 10000)
    assert j.process() == 1 and stat(j)["status"] != DECIDED, "결정 뒤 새 줄이 오면 다시 판정"

    # 11) 현재 안건이 바뀌면 이전 안건의 논의 중이 미결로 바뀐다 (모델 없이)
    c, j = fresh(llm=lambda u: answer("none"))
    feed(c, [ln(1, 0, "배포 일정 얘기"), ln(2, 4000, "배포 날짜 후보")], 5000)
    j.process()
    assert stat(j)["status"] == DISCUSSING
    feed(c, [ln(3, 8000, "비용 한도는요"), ln(4, 9000, "비용 상한 얼마")], 10000)
    n = j.calls
    st = j.board_status()
    assert st["a1"]["status"] == UNDECIDED and st["a2"]["status"] == DISCUSSING and j.calls == n

    # 12) 회의가 끝나면 결정이 아닌 안건은 미결로 확정. 줄 1개짜리도 마지막으로 한 번 판정
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"))
    feed(c, [ln(1, 0, "배포 일정 얘기")], 1000)
    assert j.process() == 0
    assert j.process(final=True) == 1 and stat(j)["status"] == UNDECIDED and "끝날 때까지" in stat(j)["reason"]["text"]
    assert j.board_status()["a2"]["status"] == NOT_STARTED, "줄이 없는 안건은 끝나도 아직 안 다룸"

    # 12-2) flush: 기다리는 규칙만 건너뛰고 종료 확정은 켜지 않는다 (종료 버튼, AI-07)
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("decided", "합의함", [(1, 1)]))
    feed(c, [ln(1, 0, "배포 일정 확정해요")], 1000)
    assert j.process() == 0 and not seen, "줄 1개는 기다린다"
    assert j.process(flush=True) == 1 and len(seen) == 1 and j.final is False, "flush는 기다리지 않지만 확정하지는 않는다"
    assert stat(j)["status"] == DECIDED
    assert j.process(flush=True) == 0, "새 줄이 없으면 flush해도 다시 부르지 않는다"

    # 12-3) preview_final: 종료를 가정한 상태를 돌려주되 판정기 상태는 바꾸지 않는다. 끝난 뒤에는 확정한 값과 같다
    c, j = fresh(llm=lambda u: answer("none"))
    feed(c, [ln(1, 0, "배포 일정 얘기"), ln(2, 4000, "배포 날짜 후보")], 5000)
    j.process()
    assert stat(j)["status"] == DISCUSSING
    pv = j.board_status(preview_final=True)
    assert pv["a1"]["status"] == UNDECIDED and "종료를 요청한" in pv["a1"]["reason"]["text"] and pv["a1"]["reason"]["evidenceSeqs"] == [2], pv
    assert pv["a2"] == {"status": NOT_STARTED, "reason": None} and j.final is False
    assert j.board_status()["a1"]["status"] == DISCUSSING, "미리보기는 판정기 상태를 바꾸지 않는다(이어서 논의하기로 돌아갈 수 있다)"
    j.process(final=True)
    assert j.board_status()["a1"]["status"] == UNDECIDED and "끝날 때까지" in j.board_status()["a1"]["reason"]["text"]
    assert j.board_status(preview_final=True) == j.board_status(), "확정한 뒤에는 미리보기와 같다"
    c, j = fresh(llm=lambda u: answer("decided", "합의함", [(1, 1)]))
    feed(c, [ln(1, 0, "배포 일정 확정해요"), ln(2, 4000, "네 좋아요 배포 일정")], 5000)
    j.process()
    assert j.board_status(preview_final=True)["a1"]["status"] == DECIDED, "결정은 미리보기에서도 결정"

    # 13) 호출이 연속 3번 실패하면 모델 판정만 멈춘다. 규칙 판정은 계속
    def boom(u):
        raise RuntimeError("키 오류 기밀단어")
    c, j = fresh(llm=boom, min_new_lines=1)
    feed(c, [ln(1, 0, "배포 얘기")], 1000)
    for k in range(5):
        feed(c, [ln(10 + k, 2000 + k * 1000, "배포 계속")], 3000 + k * 1000)
        j.process()
    assert j.halted == "연속 실패" and j.calls == 3 and j.errors == 3, (j.halted, j.calls)
    assert stat(j)["status"] in (DISCUSSING, UNDECIDED)
    # 14) 호출 상한
    c, j = fresh(llm=lambda u: answer("none"), max_calls=1)
    feed(c, [ln(1, 0, "배포 얘기"), ln(2, 1000, "배포 계속"), ln(3, 2000, "비용 얘기"), ln(4, 3000, "비용 계속")], 5000)
    j.process()
    assert j.calls == 1 and j.halted == "호출 상한"

    # 15) 안건이 목록에서 사라지면 상태를 버린다. 새로 추가된 안건은 줄이 없으면 아직 안 다룸
    c, j = fresh(llm=lambda u: answer("none"))
    feed(c, [ln(1, 0, "배포 얘기"), ln(2, 1000, "배포 계속")], 3000)
    j.process()
    c.set_agendas(2, [A2, A3])
    j.process()
    assert "a1" not in j.ag and set(j.board_status()) == {"a2", "a3"} and j.board_status()["a3"]["status"] == NOT_STARTED

    # 9-1) 해당 없음 줄이 끼면 그 뒤 맞장구는 붙이지 않는다. 사람이 고정한 낮은 확신도 줄, 간투사 줄도 넣지 않는다
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"))
    feed(c, [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 2000, "그건 그렇고 점심은 뭘 먹을까요"),
             ln(3, 3500, "네 좋아요", kind="backchannel"), ln(4, 6000, "배포 일정 확정해요"),
             ln(5, 8000, "배포 흐릿한 말", low=True), ln(6, 9000, "배포 으음 그게", kind="filler")], 10000)
    c.locks[5] = {"key": "a1"}
    c.locks[6] = {"key": "a1"}
    j.process()
    u = seen[-1].split("[전사]")[1]
    assert "네 좋아요" not in u and "흐릿한" not in u and "으음" not in u and "배포 일정 확정해요" in u, u
    # 10-1) 사람이 줄을 다른 안건으로 옮기면 남은 안건은 기다리지 않고 다시 판정한다
    c, j = fresh(llm=lambda u: answer("none"))
    feed(c, [ln(1, 0, "배포 얘기"), ln(2, 4000, "배포 계속")], 5000)
    j.process()
    n = j.calls
    c.locks[2] = {"key": "a2"}
    assert j.process() == 1 and j.calls == n + 1, "줄이 빠진 안건은 바로 다시 판정 (옮겨 온 안건은 새 줄 1개라 기다림)"
    # 13-1) 성공하면 연속 실패 횟수가 0으로 돌아간다
    plan = ["x", "x", "ok", "x", "x"]

    def flaky(u):
        r = plan.pop(0)
        if r == "x":
            raise RuntimeError("실패")
        return answer("none")
    c, j = fresh(llm=flaky, min_new_lines=1)
    for k in range(5):
        feed(c, [ln(1 + k, k * 1000, "배포 계속")], k * 1000 + 500)
        j.process()
    assert j.calls == 5 and j.errors == 4 and not j.halted, (j.calls, j.errors, j.halted)

    # 16) 모의 판정기: 형식과 근거 번호가 검증을 통과한다. 결정 표현이 없으면 판정 없음
    c, j = fresh(llm=None)
    j.llm = echo_llm
    feed(c, [ln(1, 0, "배포 날짜를 정해요"), ln(2, 4000, "배포는 목요일로 확정")], 5000)
    j.process()
    assert stat(j)["status"] == DECIDED and stat(j)["reason"]["evidenceSeqs"] == [2], stat(j)
    assert json.loads(echo_llm("[전사]\n[1] 00:00 안녕"))["verdict"] == "none"
    # 17) info 에는 내용이 없다
    info = j.info()
    assert set(info) == {"agendas", "decided", "decisionCheck", "calls", "errors", "rejected", "halted"} and info["decided"] == 1
    try:
        StatusCfg(nope=1)
        assert False
    except TypeError:
        pass

    # 18) 로그에는 전사, 안건 제목, 판정 이유가 남지 않는다
    logging.getLogger("ai-service").removeHandler(h)
    text = "\n".join(records)
    for secret in ("기밀단어", "배포 일정", "배포는 다음 주 목요일로 정함", "목요일"):
        assert secret not in text, f"로그에 내용이 남음: {secret}"
    assert records
    print("agenda_status selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
