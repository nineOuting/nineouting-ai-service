#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건 상태 판정 (AI-04) — 결정할 사항 단위

안건 안의 결정할 사항마다 상태, 결정 내용 한 줄, 근거 줄을 만들고, 안건 상태는 그 결과에서 모델 없이 규칙으로 정한다.
형식은 docs/api/agenda-status-contract.md (v0.2), 예시는 examples/api/.

결정할 사항의 상태 (보드 agendas[].decisionPoints[].status)
  NOT_STARTED    논의 전       그 사항을 다룬 줄이 없다 (모델이 none 으로 판정하거나 안건에 연결된 줄이 없다)
  UNDECIDED      미결          논의는 있었으나 결정이 없다 (모델이 정해지지 않은 내용을 한 문장으로 쓴다)
  DECIDED        결정          모델이 "답과 합의 표현이 있다"고 판정하고 결정 내용과 근거 줄이 검증을 통과했다
  DECISION_CHECK 결정 확인 필요  모델이 "답처럼 보이는 말은 있지만 확신이 낮다"고 판정하고 근거 줄이 검증을 통과했다

안건의 상태 (보드 agendas[].status) 는 사항 상태에서 정한다 (derive_status). 위에서부터 처음 맞는 줄:
  1. 연결된 줄이 없다 -> NOT_STARTED
  2. 모든 사항이 DECIDED -> DECIDED
  3. 모든 사항이 DECIDED 또는 DECISION_CHECK 이고 DECISION_CHECK 가 1개 이상 -> DECISION_CHECK
  4. 그 밖(UNDECIDED 나 NOT_STARTED 사항이 있다) -> 현재 안건이면 DISCUSSING, 아니면 UNDECIDED (회의가 끝났거나 종료를 가정하면 UNDECIDED)

동작
  - 호출은 안건당 1번에 그 안건의 모든 사항을 판정한다. 모델에는 사항마다 1부터 순번을 붙여 주고, 코드가 decisionPointId 로 되돌린다.
    번호는 AI가 만들지 않는다 (안건 목록에서 받은 값을 그대로 돌려준다).
  - 입력은 원문 줄뿐이다. 안건별 요약 문장은 쓰지 않는다 (요약 위에 쌓지 않는다).
    맞장구 줄은 합의 표현일 수 있어 넣는다. 맞장구는 분류 대상이 아니라 안건 표시가 없으므로, 바로 앞에 분류된 줄의 안건에
    붙인다(앞 줄의 끝에서 backchannel_gap_sec 이내이고, 그 사이에 해당 없음 줄이 없을 때). 간투사와 낮은 확신도 줄은 뺀다.
  - 결정(decided)·확인 필요(check)·미결(undecided)의 문장은 근거 줄이 필수다. 문장은 meeting_summary.validate_items 로 확인하고,
    통과하지 못한 사항이 있으면 오류를 알려 1회 더 부른다. 그래도 통과하지 못하면 그 사항은 결정으로 내지 않고 UNDECIDED 로
    두고 고정 문구를 쓴다. 근거 없는 결정은 내지 않는다. 근거 줄 개수에는 상한이 없다 (화면은 근거 발언을 모두 보인다).
  - 판정 시점: 줄이 새로 붙은 안건은 min_new_lines 개 이상이면 바로, 1개면 refresh_sec(음성 시간) 뒤. 줄이 빠져나갔거나 글이 바뀐
    안건, 결정할 사항의 문구·구성이 바뀐 안건, 사람이 결정 내용을 고친 안건은 기다리지 않는다. 회의가 끝나면(final) 바뀐 안건을 모두
    한 번 더 판정하고 안건 상태를 확정한다.
  - 사람이 고친 결정 내용(apply_edits): 그 사항의 결정 내용과 근거를 고정한다. 판정은 계속해서 상태만 새로 낸다.
    고친 사항의 needsCheck: 근거 줄의 글이 바뀌었거나 사라졌거나 다른 안건으로 옮겨 가면 켠다(한 번 켜지면 다시 고칠 때까지 유지).
    flag_conflict 가 켜져 있으면 AI의 판정이 결정(DECIDED, DECISION_CHECK)이 아닐 때도 켠다 (AI 판정이 바뀌면 따라 꺼진다).
  - 호출이 연속 3번 실패하거나 호출 상한에 닿으면 이 회의의 모델 판정만 멈춘다. 이미 내린 판정은 유지하고 안건 상태는 계속 도출한다.
  - 사람이 사항이나 안건의 상태를 직접 고치는 것은 받지 않는다 (받는 것은 결정 내용 문장뿐).

스레드: 일꾼 스레드(process, board_status)와 요청 스레드(apply_edits, info)가 함께 쓴다. 상태 변경은 락 안에서 하고 모델 호출은 락 밖에서 한다.
        분류기의 락은 읽을 때만 잠깐 잡고(_view), 판정기의 락과 겹쳐 잡지 않는다.

로그 규칙: 전사 내용, 안건 제목, 결정할 사항 문구, 판정 문장을 남기지 않는다. 개수와 예외 이름만 남긴다.
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
POINT_STATUSES = (NOT_STARTED, UNDECIDED, DECIDED, DECISION_CHECK)
VERDICTS = ("decided", "check", "undecided", "none")
VERDICT_STATUS = {"decided": DECIDED, "check": DECISION_CHECK, "undecided": UNDECIDED, "none": NOT_STARTED}

FIXED_NOT_STARTED = "아직 논의되지 않았어요"      # 논의 전 사항의 고정 문구 (모델 없이)


def fixed_undecided(text):
    """검증을 통과한 문장이 없는 미결 사항의 고정 문구 (모델 없이)."""
    return f"{text}는 아직 결정되지 않았어요"


SYSTEM_PROMPT = """너는 회의에서 한 안건의 결정할 사항마다 결정 여부를 판정하는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어. [맞장구]가 붙은 줄은 짧은 호응이고, [불확실]이 붙은 줄은 STT가 글자 대부분을 확신하지 못한 줄이야. [결정할 사항]에는 1부터 번호가 붙어 있어.

규칙
1. [안건]의 제목과 [결정할 사항]은 무엇을 결정해야 하는지 이해하는 데만 써. 전사에 없는 내용을 제목이나 결정할 사항에서 가져오지 마.
2. [결정할 사항]마다 하나씩, 빠짐없이 답해. n은 결정할 사항의 번호야. verdict는 넷 중 하나야.
   - "decided": 그 사항에 대한 답이 전사에 나오고, 합의 표현(동의, 확정, 정리하는 말)도 전사에서 확인될 때.
   - "check": 그 사항에 대한 답처럼 보이는 말은 있지만, 합의가 분명하지 않거나 의견이 갈린 채 남았거나 STT가 불확실해서 확신하지 못할 때.
   - "undecided": 그 사항을 다룬 말은 있지만 아직 답이 정해지지 않았을 때.
   - "none": 그 사항을 다룬 말이 전사에 없을 때.
3. 제안이나 의견만 나오고 아무도 동의하지 않았으면 decided가 아니야. check나 undecided로 해.
4. 누가 말한 내용을 다른 사람이 받아서 확인하는 말("네", "좋아요", "그렇게 해요")은 합의 표현이 될 수 있어. 앞 줄과 함께 봐.
5. verdict가 decided, check, undecided이면 text에 한 문장을 쓰고, evidence에 근거 줄 번호 구간 [[첫 번호, 끝 번호], ...]을 달아. 구간의 양 끝은 입력에 있는 번호여야 해.
   - decided: 무엇으로 결정됐는지.
   - check: 결정으로 보이는 내용.
   - undecided: 아직 정해지지 않은 내용. 의견이 갈렸으면 어떤 의견들이 나왔는지만 써.
   근거에는 그 문장의 내용이 나온 줄을 모두 넣어. decided에는 답이 나온 줄과 합의 표현이 나온 줄을 모두 넣어. 근거 줄 수에 상한은 없어.
6. text에는 근거 줄에 없는 사실, 이름, 숫자를 쓰지 마. "우세하다", "다수", "대체로"처럼 대화에 없는 평가나 짐작도 쓰지 마. 말을 인용할 때는 「」로 묶고 입력 원문과 똑같이 써(⟨ ⟩ 기호는 빼). ⟨ ⟩로 묶인 글자는 STT가 확신하지 못한 부분이야. 그 부분을 근거로 쓰면 text에 "STT 불확실"이라고 밝혀. 숫자는 말한 한글 표기를 「」로 그대로 인용해.
7. verdict가 none이면 text는 빈 문자열, evidence는 빈 목록으로 줘.
8. [불확실] 줄은 뜻이 틀렸을 수 있어. 결정할 사항에 대한 답(무엇으로 정했는지)이 [불확실]이 아닌 줄에 있고, [불확실] 줄이 "좋습니다", "네", "그렇게 해요"처럼 뜻이 분명한 짧은 동의이고 그 뜻을 뒤집는 말이 없으면 그 줄을 합의 표현으로 보고 decided로 해도 돼. 이때 text에 "STT 불확실"이라고 밝혀. 답 자체가 [불확실] 줄에만 있거나 [불확실] 줄의 뜻이 분명하지 않으면 decided가 아니라 check로 해. [불확실] 줄의 알아볼 수 없는 말은 짐작해서 쓰지 마.

출력은 JSON만. 형식:
{"points": [{"n": 1, "verdict": "decided" 또는 "check" 또는 "undecided" 또는 "none", "text": "...", "evidence": [[첫 번호, 끝 번호]]}]}"""


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
        self.show_uncertain = False   # True 면 낮은 확신도 줄을 [불확실] 표시를 달아 모델에 보여 준다 (기본 False: 예전처럼 뺀다. 2026-10-23 전후 비교에서 효과가 확인되지 않아 기본을 껐다)
        self.flag_conflict = True     # True 면 사람이 고친 사항을 AI가 결정으로 보지 않을 때도 needsCheck 를 켠다 (계약 7.3의 6번 (다), 제안)
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


# --------------------------------------------------------------------------------------
# 결정할 사항, 안건 상태 도출
# --------------------------------------------------------------------------------------
def agenda_points(a):
    """안건의 결정할 사항 목록 [{"id", "key", "position", "text"}] (position 순).
    입력 어댑터가 만든 안건에는 points 가 있다. 없으면(옛 모양) decision_point 문자열 하나를 번호 없는 사항 1개로 본다."""
    pts = a.get("points")
    if pts:
        return [dict(p) for p in sorted(pts, key=lambda p: p["position"])]
    return [{"id": None, "key": "@1", "position": 1, "text": a.get("decision_point", "")}]


def derive_status(point_statuses, has_lines, open_current):
    """사항 상태 목록에서 안건 상태를 정한다 (계약 2.2). open_current: 지금 논의 중인 현재 안건이고 아직 끝나지 않았다."""
    if not has_lines:
        return NOT_STARTED
    st = list(point_statuses)
    if st and all(s == DECIDED for s in st):
        return DECIDED
    if st and all(s in (DECIDED, DECISION_CHECK) for s in st) and DECISION_CHECK in st:
        return DECISION_CHECK
    return DISCUSSING if open_current else UNDECIDED


def _reason_text(stat_list):
    """안건의 statusReason 문장. 코드가 만들고 사항 개수만 담는다 (내용은 decisionText 가 담는다)."""
    t = len(stat_list)
    d, c = stat_list.count(DECIDED), stat_list.count(DECISION_CHECK)
    u, s = stat_list.count(UNDECIDED), stat_list.count(NOT_STARTED)
    if d == t:
        return f"결정할 사항 {t}개가 모두 결정됨"
    parts = [f"{name} {n}개" for name, n in (("결정 확인 필요", c), ("미결", u), ("논의 전", s)) if n]
    return f"결정할 사항 {t}개 중 {d}개가 결정됨" + (f" ({', '.join(parts)})" if parts else "")


# --------------------------------------------------------------------------------------
# 모의 판정기 (모델을 부르지 않는다. 비용 0)
# --------------------------------------------------------------------------------------
def echo_llm(user):
    """마지막 줄의 말투로 정하는 모의 판정기. 구조(JSON 형식, 근거 번호)만 맞추고, 모든 결정할 사항에 같은 판정을 낸다.
    마지막 줄에 '확정'·'결정'·'하기로'가 있으면 decided, '글쎄'·'아마'가 있으면 check, 아니면 none."""
    head, _sep, tail = user.partition("[전사]\n")
    n_pts = max(1, len(re.findall(r"^\d+\. ", head.split("[결정할 사항]", 1)[-1], re.M)))
    rows = re.findall(r"^\[(\d+)\] (\d\d:\d\d) (.*)$", tail.replace("[불확실] ", ""), re.M)
    v, why, ev = "none", "", []
    if rows:
        n, _t, text = rows[-1]
        text = re.sub(r"[⟨⟩]", "", text)
        if any(w in text for w in ("확정", "결정", "하기로")):
            v, why, ev = "decided", "마지막 줄에 결정 표현이 나옴", [[int(n), int(n)]]
        elif any(w in text for w in ("글쎄", "아마")):
            v, why, ev = "check", "마지막 줄에 불확실한 표현이 나옴", [[int(n), int(n)]]
    return json.dumps({"points": [{"n": k, "verdict": v, "text": why, "evidence": [list(e) for e in ev]}
                                  for k in range(1, n_pts + 1)]}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, SYSTEM_PROMPT, user, task=TASK)


def _tag(ln):
    """모델에 보여 줄 줄 표시: 맞장구는 [맞장구], 낮은 확신도는 [불확실]."""
    return ("[맞장구] " if ln.get("kind") == "backchannel" else "") + ("[불확실] " if ln.get("low_confidence") else "")


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:10]


def _new_state():
    return {"pts": {}, "reflected": {}, "wait_since": None, "dirty": False, "sig": None}


def _parse_points(raw, n):
    """모델 응답 -> {사항 순번: (verdict, text, evidence)}. 형식이 틀리면 ValueError (메시지에는 입력 내용을 넣지 않는다)."""
    d = parse_llm_json(raw)
    arr = d.get("points")
    if not isinstance(arr, list):
        raise ValueError("points 가 목록이 아님")
    out = {}
    for it in arr:
        if not isinstance(it, dict):
            raise ValueError("points 항목이 객체가 아님")
        k = it.get("n")
        if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= n:
            raise ValueError(f"n 은 1부터 {n} 사이의 번호여야 함")
        if k in out:
            raise ValueError("n 이 겹침")
        verdict = it.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError("verdict 는 decided, check, undecided, none 중 하나여야 함")
        text, evidence = it.get("text"), it.get("evidence")
        if verdict != "none":
            if not isinstance(text, str) or not text.strip():
                raise ValueError("decided, check, undecided 에는 text 가 필요함")
            if not isinstance(evidence, list):
                raise ValueError("evidence 가 목록이 아님")
        out[k] = (verdict, text.strip() if isinstance(text, str) else "", evidence or [])
    if len(out) != n:
        raise ValueError(f"결정할 사항 {n}개 모두에 하나씩 답해야 함")
    return out


class AgendaStatusJudge:
    def __init__(self, classifier, cfg=None, llm=None):
        self.c = classifier
        self.cfg = cfg or StatusCfg()
        self.llm = llm or make_llm(self.cfg)
        self.lock = threading.RLock()
        self.ag = {}                  # 안건 key -> 상태 {"pts", "reflected", "wait_since", "dirty", "sig"}. pts: 사항 key -> {"status", "text", "evidence", "fixed"}
        self.known = set()            # 안건 목록에 한 번이라도 있었던 key (사라졌는지 가리는 데 쓴다)
        self.known_points = set()     # 안건 목록에 한 번이라도 있었던 (안건 key, 사항 key)
        self.edits = {}               # (안건 key, 사항 key) -> 사람이 고친 결정 내용 {"text", "evidence", "needs_check", "sig", "ack"}
        self.edit_rev = {}            # (안건 key, 사항 key) -> 마지막으로 반영한 rev
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.rejected = 0             # 문장 검증을 통과하지 못해 결정으로 내지 않고 고정 문구로 대신한 사항 수
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
            deleted = set(c.store.deleted)
        rows, last_key, last_end = {}, None, None
        for ln in snap:
            low = bool(ln.get("low_confidence"))
            if ln.get("kind") == "filler" or (low and not self.cfg.show_uncertain):
                continue
            if ln.get("kind") == "backchannel" or (low and ln["id"] not in classified):
                # 맞장구와 낮은 확신도 줄은 분류 대상이 아니라 안건 표시가 없다: 바로 앞에 분류된 줄의 안건에 붙인다
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
        return {"agendas": agendas, "keys": keys, "rows": rows, "current": current, "ids": ids, "now_ms": now_ms,
                "eff": eff, "by_id": {ln["id"]: ln["text"] for ln in snap}, "deleted": deleted}

    # ---------------- 사람이 고친 결정 내용 ----------------
    @staticmethod
    def _moved(e, akey, view):
        """근거 줄 중 분류가 정해졌고 이 안건이 아닌 줄."""
        eff = view["eff"]
        return {s for s in e["evidence"] if s in eff and eff[s] != akey}

    def _ack(self, e, akey, view):
        """사용자가 이미 알고 있는 변화: 지금 옮겨 갔거나 사라진 근거 줄. 이 줄들 때문에 다시 확인 필요를 켜지 않는다."""
        return frozenset(self._moved(e, akey, view) | {s for s in e["evidence"] if s in view["deleted"]})

    def apply_edits(self, edits):
        """edits: [{"agenda_id", "point_id", "text", "evidence_seqs", "rev"}] (어댑터를 거친 것).
        반환: {"applied": 새로 반영한 수, "ignored": [{"decisionPointId", "reason": "stale"}]}.
        받은 사항은 고정한다: 결정 내용과 근거를 덮어쓰지 않고 상태만 계속 판정한다. 모르는 안건·사항도 보관한다."""
        with self.c.lock:
            for e in edits:
                self.c.orig.setdefault(str(e["agenda_id"]), e["agenda_id"])   # 안건 목록보다 먼저 와도 같은 타입으로 돌려주기 위해
        view = self._view()
        applied, ignored = 0, []
        with self.lock:
            for e in edits:
                ek = (str(e["agenda_id"]), str(e["point_id"]))
                if e["rev"] <= self.edit_rev.get(ek, e["rev"] - 1):
                    ignored.append({"decisionPointId": e["point_id"], "reason": "stale"})
                    continue
                ent = {"text": e["text"], "evidence": sorted(set(e["evidence_seqs"])), "needs_check": False, "sig": None}
                ent["ack"] = self._ack(ent, ek[0], view)
                self.edits[ek] = ent
                self.edit_rev[ek] = e["rev"]
                self.ag.setdefault(ek[0], _new_state())["dirty"] = True    # 다음 주기에 기다리지 않고 다시 판정
                applied += 1
        log.info("고친 결정 내용 반영 meeting=%s edits=%s applied=%s", self.c.meeting_id, len(edits), applied)
        return {"applied": applied, "ignored": ignored}

    def _refresh_flags(self, view):
        """고친 사항의 needsCheck 를 켠다 (계약 7.3의 6번 (가)(나)). 한 번 켜지면 다시 고칠 때까지 유지한다."""
        by_id, deleted = view["by_id"], view["deleted"]
        for (akey, _pk), e in self.edits.items():
            if e["needs_check"]:
                continue
            ev = e["evidence"]
            if any(s in deleted and s not in e["ack"] for s in ev):
                e["needs_check"] = True
                continue
            if ev and all(s in by_id for s in ev):            # 아직 오지 않은 줄이 있으면 비교하지 않는다 (재시작 직후)
                sig = tuple((s, _h(by_id[s])) for s in ev)
                if e["sig"] is None:
                    e["sig"] = sig
                elif sig != e["sig"]:
                    e["needs_check"] = True
                    continue
            if self._moved(e, akey, view) - e["ack"]:
                e["needs_check"] = True

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
        for ek in [ek for ek in self.edits if ek[0] in self.known and ek[0] not in keys]:
            del self.edits[ek]                               # 사라진 안건의 고친 결정 내용도 버린다
            self.edit_rev.pop(ek, None)
        cur_pts = {(a["key"], p["key"]) for a in view["agendas"] for p in agenda_points(a)}
        for ek in [ek for ek in self.edits if ek in self.known_points and ek not in cur_pts and ek[0] in keys]:
            del self.edits[ek]                               # 사라진 사항의 고친 결정 내용도 버린다
            self.edit_rev.pop(ek, None)
        self.known |= keys
        self.known_points |= cur_pts
        jobs = []
        for a in view["agendas"]:
            key = a["key"]
            st = self.ag.setdefault(key, _new_state())
            rows = view["rows"].get(key, [])
            if not rows:                                     # 줄이 없다: 판정을 비운다 (모델 없이 아직 안 다룸)
                st.update(pts={}, reflected={}, wait_since=None, dirty=False, sig=None)
                continue
            points = agenda_points(a)
            psig = tuple((p["key"], p["text"]) for p in points)
            if st["sig"] is not None and st["sig"] != psig:
                st["dirty"] = True                           # 결정할 사항의 문구나 구성이 바뀜: 기다리지 않는다
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
            jobs.append(self._make_job(a, points, psig, rows, cur))
            st["dirty"] = False
        return jobs

    def _make_job(self, agenda, points, psig, rows, cur):
        K = rows[-self.cfg.max_lines:]
        parts = [f"다음은 하나의 안건에 연결된 회의 발언이야 (줄 {len(K)}개). [맞장구]가 붙은 줄은 짧은 호응이고, [불확실]이 붙은 줄은 STT가 확신하지 못한 줄이야.",
                 f"[안건]\n제목: {_clean(agenda['title'])}\n[결정할 사항]\n" + "\n".join(f"{n}. {_clean(p['text'])}" for n, p in enumerate(points, 1)),
                 "[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {_tag(ln)}{ln['text_marked']}"
                                       for k, (ln, _bc) in enumerate(K, 1))]
        return {"key": agenda["key"], "points": [{"key": p["key"]} for p in points], "psig": psig,
                "lines": [ln for ln, _bc in K], "reflected": cur, "user": "\n\n".join(parts)}

    def _run_job(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur_user = job["user"], job["user"]
        n = len(job["points"])
        best, failed, calls = {}, False, 0   # best: 사항 순번 -> (verdict, text, 근거 줄 id 목록). 검증을 통과한 결과만 담는다
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    log.warning("상태 판정 호출 상한에 도달해 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
                    failed = True
                    break
                self.calls += 1
            calls += 1
            try:
                raw = self.llm(cur_user)
            except Exception as e:                           # 키 오류, 네트워크 오류, 사용량 제한 등
                with self.lock:
                    self._fail(type(e).__name__)
                failed = True
                break
            try:
                res = _parse_points(raw, n)
            except Exception as e:
                if attempt == 2:
                    with self.lock:
                        self._fail("ParseError")
                    failed = True
                    break
                why = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else type(e).__name__
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\n형식 오류({why}). 형식에 맞게 JSON만 다시 출력해."
                continue
            errs_by = {}
            for k in sorted(res):
                verdict, text, evidence = res[k]
                if verdict == "none":
                    best[k] = ("none", "", [])
                    continue
                (_it, errs, _warns, spans), = validate_items([{"text": text, "topic": None, "evidence": evidence}], job["lines"])
                if errs:
                    errs_by[k] = errs                        # 이번 응답이 틀렸어도 앞서 통과한 결과가 있으면 그것을 쓴다
                    continue
                ids = sorted({job["lines"][i - 1]["id"] for a, b in spans for i in range(a, b + 1)})
                best[k] = (verdict, text, ids)
            if not errs_by or attempt == 2:
                break
            msg = "\n".join(f"- 결정할 사항 {k}: {e}" for k in sorted(errs_by) for e in errs_by[k])
            cur_user = user + (f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg}\n"
                               "오류를 고쳐서 JSON을 다시 출력해. 근거를 댈 수 없으면 verdict를 none으로 줘.")
        with self.lock:
            st = self.ag.get(job["key"])
            if failed:                                       # 실패: 반영하지 않고 다음 주기에 다시 한다
                if st is not None:
                    st["dirty"] = True
                return calls
            self.err_streak = 0
            if st is not None:
                self._commit(job, st, {k: best.get(k, ("rejected", "", [])) for k in range(1, n + 1)})
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("상태 판정 호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("상태 판정을 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    def _commit(self, job, st, outcome):
        """outcome: 사항 순번 -> (verdict 또는 "rejected", text, 근거 줄 id 목록)."""
        counts = {}
        for k, p in enumerate(job["points"], 1):
            verdict, text, ids = outcome[k]
            if verdict == "rejected":                        # 근거 없는 결정은 내지 않는다: 미결 고정 문구로 대신한다
                self.rejected += 1
                st["pts"][p["key"]] = {"status": UNDECIDED, "text": "", "evidence": [], "fixed": True}
            else:
                st["pts"][p["key"]] = {"status": VERDICT_STATUS[verdict], "text": text, "evidence": list(ids), "fixed": False}
            counts[verdict] = counts.get(verdict, 0) + 1
        live = {p["key"] for p in job["points"]}
        for pk in [pk for pk in st["pts"] if pk not in live]:
            del st["pts"][pk]                                # 목록에서 사라진 사항의 결과
        st["reflected"], st["wait_since"], st["sig"] = job["reflected"], None, job["psig"]
        if counts.get("rejected"):
            log.info("문장 검증 탈락 meeting=%s rejected=%s", self.c.meeting_id, self.rejected)
        log.info("안건 상태 판정 meeting=%s points=%s decided=%s check=%s undecided=%s calls=%s", self.c.meeting_id, len(job["points"]),
                 counts.get("decided", 0), counts.get("check", 0), counts.get("undecided", 0) + counts.get("rejected", 0), self.calls)

    # ---------------- 출력 ----------------
    def _point_out(self, akey, p, st, has_lines):
        """사항 하나의 보드 항목. 반환: (보드 항목, AI가 판정한 상태, AI가 낸 근거 줄)."""
        r = st["pts"].get(p["key"]) if has_lines else None
        status = r["status"] if r else NOT_STARTED
        ai_ev = list(r["evidence"]) if r else []
        e = self.edits.get((akey, p["key"]))
        if e is not None:
            text, ev = e["text"], list(e["evidence"])
            conflict = self.cfg.flag_conflict and has_lines and status not in (DECIDED, DECISION_CHECK)
            need = bool(e["needs_check"] or conflict)
        else:
            if r is not None and r["fixed"]:
                text, ev = fixed_undecided(p["text"]), []
            elif status == NOT_STARTED:
                text, ev = FIXED_NOT_STARTED, []
            else:
                text, ev = r["text"], list(r["evidence"])
            need = False
        return ({"decisionPointId": p["id"], "status": status, "decisionText": text, "evidenceSeqs": ev,
                 "edited": e is not None, "needsCheck": need}, status, ai_ev)

    def board_status(self, preview_final=False):
        """안건 key -> {"status", "reason": {"text", "evidenceSeqs"} 또는 None, "points": [보드의 decisionPoints 항목]}.
        안건 보드에 합쳐 보낸다. 안건 상태는 사항 상태에서 규칙으로 정하고(derive_status), reason 은 코드가 만든 문장이다.
        preview_final: 종료 확정을 가정한 값을 돌려준다(논의 중이던 안건은 미결). 판정기 상태는 바꾸지 않는다.
        종료 버튼을 눌렀을 때 미결 목록(AI-07)을 만드는 데 쓴다. 이어서 논의하기로 돌아가면 보드의 상태는 그대로 논의 중이다."""
        view = self._view()
        with self.lock:
            self._refresh_flags(view)
            out = {}
            for a in view["agendas"]:
                key = a["key"]
                st = self.ag.get(key) or _new_state()
                rows = view["rows"].get(key, [])
                outs, stats, evs = [], [], []
                for p in agenda_points(a):
                    po, s, ev = self._point_out(key, p, st, bool(rows))
                    outs.append(po)
                    stats.append(s)
                    evs.append(ev)
                if not rows:
                    out[key] = {"status": NOT_STARTED, "reason": None, "points": outs}
                    continue
                open_current = view["current"] == key and not self.final and not preview_final
                status = derive_status(stats, True, open_current)
                if status in (DECIDED, DECISION_CHECK):
                    ids = sorted({s for s_, ev in zip(stats, evs) if s_ in (DECIDED, DECISION_CHECK) for s in ev})
                else:
                    ids = []
                if not ids:                                  # 규칙 근거: 연결된 마지막 발언 줄 (불확실 줄은 뺀다)
                    sure = [r for r in rows if not r[0].get("low_confidence")] or rows
                    ids = [sure[-1][0]["id"]]
                out[key] = {"status": status, "reason": {"text": _reason_text(stats), "evidenceSeqs": ids}, "points": outs}
            return out

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 결정할 사항 문구, 판정 문장은 넣지 않는다.
        decided, decisionCheck 는 판정된 결정할 사항의 수다 (안건 수가 아니다)."""
        with self.lock:
            pts = [r for st in self.ag.values() for r in st["pts"].values()]
            return {"agendas": len(self.ag),
                    "decided": sum(1 for r in pts if r["status"] == DECIDED),
                    "decisionCheck": sum(1 for r in pts if r["status"] == DECISION_CHECK),
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

    def feed(c, lines, sent_ms, edits=()):
        c.receive({"lines": lines, "edits": list(edits), "sent_ms": sent_ms})
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

    def answer_pts(items):
        """결정할 사항마다 (verdict, text, 근거 구간들) -> 모델 응답"""
        return json.dumps({"points": [{"n": k, "verdict": v, "text": tx, "evidence": [list(e) for e in ev]}
                                      for k, (v, tx, ev) in enumerate(items, 1)]}, ensure_ascii=False)

    def answer(verdict, reason="", ev=()):
        return answer_pts([(verdict, reason, ev)])

    # 1) 줄이 없으면 아직 안 다룸. 모델을 부르지 않는다
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"))
    assert j.process() == 0 and not seen
    bs = j.board_status()
    assert bs["a1"]["status"] == NOT_STARTED and bs["a1"]["reason"] is None and bs["a2"]["status"] == NOT_STARTED
    assert bs["a1"]["points"] == [{"decisionPointId": None, "status": NOT_STARTED, "decisionText": FIXED_NOT_STARTED,
                                   "evidenceSeqs": [], "edited": False, "needsCheck": False}], bs["a1"]["points"]

    # 2) 줄이 새로 2개 이상이면 바로 판정. 결정 없음이면 현재 안건은 논의 중, 아니면 미결 (모델 없이)
    feed(c, [ln(1, 0, "배포는 언제 할까요"), ln(2, 4000, "배포 날짜 후보를 모아봐요"), ln(3, 8000, "비용 한도 얘기해요")], 9000)
    assert j.process() == 1 and len(seen) == 1, "새 줄이 2개인 안건 하나만 판정"
    assert "제목: 배포 일정" in seen[0] and "1. 배포 날짜를 확정한다" in seen[0] and "[1] 00:00" in seen[0]
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
    assert s["status"] == DECIDED and s["reason"] == {"text": "결정할 사항 1개가 모두 결정됨", "evidenceSeqs": [1, 2]}, s
    assert s["points"] == [{"decisionPointId": None, "status": DECIDED, "decisionText": "배포는 다음 주 목요일로 정함",
                            "evidenceSeqs": [1, 2], "edited": False, "needsCheck": False}], s
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
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"), show_uncertain=False)
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
    assert j.process(final=True) == 1 and stat(j)["status"] == UNDECIDED and "결정할 사항 1개 중 0개가 결정됨" in stat(j)["reason"]["text"]
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
    assert pv["a1"]["status"] == UNDECIDED and pv["a1"]["reason"]["evidenceSeqs"] == [2], pv
    assert pv["a2"]["status"] == NOT_STARTED and pv["a2"]["reason"] is None and j.final is False
    assert j.board_status()["a1"]["status"] == DISCUSSING, "미리보기는 판정기 상태를 바꾸지 않는다(이어서 논의하기로 돌아갈 수 있다)"
    j.process(final=True)
    assert j.board_status()["a1"]["status"] == UNDECIDED and j.board_status()["a1"]["reason"]["evidenceSeqs"] == [2]
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
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"), show_uncertain=False)
    feed(c, [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 2000, "그건 그렇고 점심은 뭘 먹을까요"),
             ln(3, 3500, "네 좋아요", kind="backchannel"), ln(4, 6000, "배포 일정 확정해요"),
             ln(5, 8000, "배포 흐릿한 말", low=True), ln(6, 9000, "배포 으음 그게", kind="filler")], 10000)
    c.locks[5] = {"key": "a1"}
    c.locks[6] = {"key": "a1"}
    j.process()
    u = seen[-1].split("[전사]")[1]
    assert "네 좋아요" not in u and "흐릿한" not in u and "으음" not in u and "배포 일정 확정해요" in u, u
    # 9-2) 낮은 확신도 줄은 [불확실] 표시를 달아 모델에 보여 준다 (앞 줄의 안건에 붙인다. 간격이 길거나 해당 없음 줄이 끼면 붙이지 않는다. 간투사는 뺀다)
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"), show_uncertain=True)
    feed(c, [ln(1, 0, "배포는 목요일로 하면 되나요"), ln(2, 2500, "좋습니다", low=True),
             ln(3, 4000, "배포 일정 확정해요"), ln(4, 5500, "음", kind="filler"), ln(5, 6000, "네 아주 좋습니다", kind="backchannel", low=True),
             ln(6, 30000, "멀리 떨어진 흐릿한 말", low=True)], 31000)
    j.process()
    u = seen[-1].split("[전사]")[1]
    assert "[불확실] 좋습니다" in u and "[맞장구] [불확실] 네 아주 좋습니다" in u and "배포 일정 확정해요" in u, u
    assert "멀리" not in u and "음" not in u.replace("좋습니다", "").replace("합니다", ""), "간격이 길면 붙이지 않고 간투사는 뺀다: " + u
    assert "[1] 00:00 배포는" in u and "[2] 00:02 [불확실] 좋습니다" in u
    # 규칙 문구의 줄 수와 근거는 불확실 줄을 뺀다 (미결 문구가 잡음 줄을 가리키지 않게)
    c.set_agendas(2, [A2, A3])
    j.process()
    bs2 = j.board_status()
    assert "a1" not in bs2
    c, j = fresh(llm=lambda u: answer("none"))
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 2000, "배포 동의해요"), ln(3, 3500, "흐릿", low=True)], 5000)
    j.process()
    c.set_agendas(2, [A2, A3, A1])
    j.final = True
    r = j.board_status()["a1"]["reason"]
    assert r["evidenceSeqs"] == [2] and r["text"].startswith("결정할 사항 1개 중 0개가 결정됨"), r
    # 불확실 줄이 합의 표현이면 모델이 결정으로 낼 수 있고, 불확실 줄을 근거로 달 수 있다
    c, j = fresh(llm=lambda u: answer("decided", "배포를 목요일로 하기로 함 (동의 표현은 「좋습니다」, STT 불확실)", [(1, 2)]), show_uncertain=True)
    feed(c, [ln(1, 0, "배포를 목요일로 해도 되나요"), ln(2, 2500, "좋습니다", low=True)], 4000)
    j.process()
    assert stat(j)["status"] == DECIDED and stat(j)["reason"]["evidenceSeqs"] == [1, 2], stat(j)
    # 모의 판정기는 [불확실] 표시를 무시하고 마지막 줄의 말투로 정한다. 프롬프트가 [불확실] 줄의 처리를 말해 준다
    assert json.loads(echo_llm("[전사]\n[1] 00:00 [불확실] 배포 확정해요"))["points"][0]["verdict"] == "decided"
    assert "[불확실]" in SYSTEM_PROMPT and "8. [불확실] 줄" in SYSTEM_PROMPT and 'text에 "STT 불확실"' in SYSTEM_PROMPT
    # 설정을 끄면 예전처럼 뺀다
    seen = []
    c, j = fresh(llm=lambda u: seen.append(u) or answer("none"), show_uncertain=False)
    feed(c, [ln(1, 0, "배포는 목요일로 하면 되나요"), ln(2, 2500, "좋습니다", low=True), ln(3, 4000, "배포 일정 확정해요")], 5000)
    j.process()
    assert "불확실" not in seen[-1].split("[전사]")[1] and "좋습니다" not in seen[-1].split("[전사]")[1]
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
    assert json.loads(echo_llm("[전사]\n[1] 00:00 안녕"))["points"][0]["verdict"] == "none"
    # 17) info 에는 내용이 없다
    info = j.info()
    assert set(info) == {"agendas", "decided", "decisionCheck", "calls", "errors", "rejected", "halted"} and info["decided"] == 1
    try:
        StatusCfg(nope=1)
        assert False
    except TypeError:
        pass

    # ---------------------------------------------------------------- 결정할 사항 단위 (v0.2)
    def agp(i, title, pts, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": " / ".join(t for _p, t in pts),
                "added": False, "points": [{"id": pid, "key": str(pid), "position": n, "text": tx} for n, (pid, tx) in enumerate(pts, 1)]}
    PTS = [(7, "1차 배포 날짜 확정"), (8, "배포 담당 지정"), (9, "사항기밀 점검 항목 확정")]
    P1 = agp("p1", "배포 일정", PTS, 1)
    L4 = [ln(1, 0, "배포 날짜는 다음 주 목요일 어때요"), ln(2, 2000, "배포 목요일로 해요 확정"),
          ln(3, 4000, "배포 담당은 백엔드 쪽이 맡을 것 같아요"), ln(4, 6000, "배포 점검 항목은 더 이야기해 봐야 해요")]
    ANS3 = lambda: answer_pts([("decided", "1차 배포는 다음 주 목요일로 정함", [(1, 2)]),
                               ("check", "담당은 백엔드 쪽으로 보임", [(3, 3)]),
                               ("undecided", "점검 항목은 아직 정해지지 않음", [(4, 4)])])

    # 19) 사항이 여러 개인 안건: 한 번에 사항 전체를 판정하고, 번호는 받은 값을 타입 그대로 돌려준다. 안건 상태는 규칙으로 정한다
    seen = []
    c, j = fresh(agendas=(P1, A2), llm=lambda u: seen.append(u) or ANS3())
    feed(c, L4, 7000)
    assert j.process() == 1 and len(seen) == 1, "사항이 3개여도 호출은 안건당 1번"
    assert "1. 1차 배포 날짜 확정" in seen[0] and "2. 배포 담당 지정" in seen[0] and "3. 사항기밀 점검 항목 확정" in seen[0], seen[0]
    bs = j.board_status()["p1"]
    pts = bs["points"]
    assert [p["decisionPointId"] for p in pts] == [7, 8, 9], "번호는 받은 값 그대로(정수)"
    assert [p["status"] for p in pts] == [DECIDED, DECISION_CHECK, UNDECIDED]
    assert [p["evidenceSeqs"] for p in pts] == [[1, 2], [3], [4]]
    assert pts[0]["decisionText"] == "1차 배포는 다음 주 목요일로 정함" and not any(p["edited"] or p["needsCheck"] for p in pts)
    assert bs["status"] == DISCUSSING, "일부만 결정이면 결정이 아니고, 현재 안건이면 논의 중"
    assert bs["reason"] == {"text": "결정할 사항 3개 중 1개가 결정됨 (결정 확인 필요 1개, 미결 1개)", "evidenceSeqs": [4]}, bs["reason"]
    assert j.process() == 0, "같은 내용은 다시 부르지 않음"
    j.process(final=True)
    assert j.board_status()["p1"]["status"] == UNDECIDED, "회의가 끝나면 일부만 결정된 안건은 미결"
    assert j.board_status()["p1"] == j.board_status(preview_final=True)["p1"]

    # 19-1) 안건 상태 도출 규칙 (계약 2.2)
    Dd, Kk, Uu, Nn = DECIDED, DECISION_CHECK, UNDECIDED, NOT_STARTED
    for sts, has, cur, want in [([Dd, Dd, Dd], True, False, DECIDED), ([Dd, Dd, Dd], True, True, DECIDED),
                                ([Dd, Kk], True, True, DECISION_CHECK), ([Kk, Kk], True, False, DECISION_CHECK),
                                ([Dd, Kk, Uu], True, False, UNDECIDED), ([Dd, Kk, Uu], True, True, DISCUSSING),
                                ([Dd, Nn], True, True, DISCUSSING), ([Dd, Nn], True, False, UNDECIDED),
                                ([Uu], True, False, UNDECIDED), ([Nn, Nn], True, True, DISCUSSING),
                                ([Dd, Dd], False, True, NOT_STARTED), ([Kk], False, False, NOT_STARTED)]:
        assert derive_status(sts, has, cur) == want, (sts, has, cur, want)

    # 19-2) none 은 논의 전 고정 문구. 근거 번호가 틀린 사항은 고쳐 부르고, 앞서 통과한 사항은 그대로 둔다
    inputs = []
    plan = [answer_pts([("decided", "배포는 목요일로 정함", [(1, 2)]), ("check", "담당 불명", [(9, 9)]), ("none", "", [])]),
            answer_pts([("decided", "배포는 목요일로 정함", [(9, 9)]), ("check", "담당이 불확실함", [(3, 3)]), ("none", "", [])])]
    c, j = fresh(agendas=(P1, A2), llm=lambda u: inputs.append(u) or plan.pop(0))
    feed(c, L4, 7000)
    assert j.process() == 2 and "결정할 사항 2:" in inputs[1] and "결정할 사항 1:" not in inputs[1], inputs[1]
    pts = j.board_status()["p1"]["points"]
    assert [p["status"] for p in pts] == [DECIDED, DECISION_CHECK, NOT_STARTED] and j.rejected == 0
    assert pts[0]["evidenceSeqs"] == [1, 2], "다시 부른 응답이 틀려도 앞서 통과한 결과를 쓴다"
    assert pts[1]["evidenceSeqs"] == [3] and pts[2]["decisionText"] == FIXED_NOT_STARTED and pts[2]["evidenceSeqs"] == []

    # 19-3) 끝내 검증을 통과하지 못한 사항은 결정으로 내지 않고 미결 고정 문구로 대신한다 (근거 없는 미결 문장도 버린다)
    plan = [answer_pts([("decided", "배포는 목요일로 정함", [(9, 9)]), ("undecided", "근거를 대지 않은 문장", []),
                        ("undecided", "점검 항목은 아직 정해지지 않음", [(4, 4)])])] * 2
    c, j = fresh(agendas=(P1, A2), llm=lambda u: plan.pop(0))
    feed(c, L4, 7000)
    assert j.process() == 2
    pts = j.board_status()["p1"]["points"]
    assert [p["status"] for p in pts] == [UNDECIDED, UNDECIDED, UNDECIDED] and j.rejected == 2 and j.err_streak == 0
    assert pts[0]["decisionText"] == "1차 배포 날짜 확정는 아직 결정되지 않았어요" and pts[0]["evidenceSeqs"] == []
    assert pts[1]["decisionText"] == "배포 담당 지정는 아직 결정되지 않았어요"
    assert pts[2]["decisionText"] == "점검 항목은 아직 정해지지 않음" and pts[2]["evidenceSeqs"] == [4], "통과한 사항은 그대로"
    # 19-4) 모든 사항에 답하지 않았거나 n 이 틀리면 형식 오류: 한 번 더, 둘 다 틀리면 실패
    plan = [answer_pts([("none", "", [])]), json.dumps({"points": [{"n": 5, "verdict": "none", "text": "", "evidence": []}]})]
    c, j = fresh(agendas=(P1, A2), llm=lambda u: plan.pop(0))
    feed(c, L4, 7000)
    assert j.process() == 2 and j.errors == 1 and j.board_status()["p1"]["points"][0]["status"] == NOT_STARTED
    # 19-5) 근거 줄 개수에는 상한이 없다 (화면은 근거 발언을 모두 보인다)
    P3 = agp("p3", "배포 일정", [(21, "배포 방식 확정")], 1)
    c, j = fresh(agendas=(P3, A2), llm=lambda u: answer("decided", "배포 방식을 정함", [(1, 40)]))
    feed(c, [ln(i, i * 1000, f"배포 이야기 {i}") for i in range(1, 41)], 41000)
    j.process()
    p = j.board_status()["p3"]["points"][0]
    assert p["status"] == DECIDED and p["evidenceSeqs"] == list(range(1, 41)), len(p["evidenceSeqs"])

    # 19-6) 같은 n 이 두 번 나오면, 모든 사항이 한 번씩은 채워져도 형식 오류다 (한 번 더, 둘 다 틀리면 실패)
    dup = json.dumps({"points": [{"n": 1, "verdict": "none", "text": "", "evidence": []}, {"n": 1, "verdict": "none", "text": "", "evidence": []},
                                 {"n": 2, "verdict": "none", "text": "", "evidence": []}, {"n": 3, "verdict": "none", "text": "", "evidence": []}]})
    c, j = fresh(agendas=(P1, A2), llm=lambda u: dup)
    feed(c, L4, 7000)
    assert j.process() == 2 and j.errors == 1, "n 이 겹친 응답은 형식 오류"
    # 19-7) 결정 확인 필요 안건의 근거는 결정·확인 필요 사항의 근거 줄을 합친 것이다 (마지막 줄이 아니다)
    P4 = agp("p4", "배포 일정", [(31, "배포 방식 확정"), (32, "배포 담당 지정")], 1)
    c, j = fresh(agendas=(P4, A2), llm=lambda u: answer_pts([("decided", "방식은 점진 배포", [(1, 1)]), ("check", "담당은 백엔드로 보임", [(2, 2)])]))
    feed(c, [ln(1, 0, "배포 방식은 점진 배포로 해요"), ln(2, 2000, "배포 담당은 백엔드가 맡을 듯해요"),
             ln(3, 4000, "배포 일정 이야기 3번"), ln(4, 6000, "배포 일정 이야기 4번")], 7000)
    j.process()
    bs = j.board_status()["p4"]
    assert bs["status"] == DECISION_CHECK and bs["reason"] == {"text": "결정할 사항 2개 중 1개가 결정됨 (결정 확인 필요 1개)", "evidenceSeqs": [1, 2]}, bs["reason"]

    # 20) 사람이 고친 결정 내용: 고정하고 상태만 계속 판정한다
    def edit(rev, text="담당은 인프라 팀으로 확정", ev=(3,), pid=8, aid="p1"):
        return {"agenda_id": aid, "point_id": pid, "text": text, "evidence_seqs": list(ev), "rev": rev}
    plan = [ANS3()]
    c, j = fresh(agendas=(P1, A2), llm=lambda u: plan.pop(0))
    feed(c, L4, 7000)
    j.process()
    r = j.apply_edits([edit(1)])
    assert r == {"applied": 1, "ignored": []}
    p = j.board_status()["p1"]["points"][1]
    assert p == {"decisionPointId": 8, "status": DECISION_CHECK, "decisionText": "담당은 인프라 팀으로 확정",
                 "evidenceSeqs": [3], "edited": True, "needsCheck": False}, p
    assert j.apply_edits([edit(1)]) == {"applied": 0, "ignored": [{"decisionPointId": 8, "reason": "stale"}]}
    assert j.apply_edits([edit(0, text="옛 글")])["applied"] == 0 and j.board_status()["p1"]["points"][1]["decisionText"] == "담당은 인프라 팀으로 확정"
    # 20-1) 고친 사항이 있는 안건은 기다리지 않고 다시 판정한다. 고친 글과 근거는 덮어쓰지 않고, 상태만 새로 낸다
    plan = [answer_pts([("decided", "새 문장", [(1, 2)]), ("undecided", "담당이 갈림", [(3, 3)]), ("none", "", [])])]
    assert j.process() == 1
    pts = j.board_status()["p1"]["points"]
    assert pts[1]["status"] == UNDECIDED and pts[1]["decisionText"] == "담당은 인프라 팀으로 확정" and pts[1]["evidenceSeqs"] == [3] and pts[1]["edited"]
    assert pts[0]["decisionText"] == "새 문장" and not pts[0]["edited"], "고치지 않은 사항은 새 판정으로 바뀐다"
    # 사람이 결정으로 적었는데 AI는 결정을 못 찾았으면 확인 필요 (flag_conflict). 끄면 켜지지 않는다
    assert pts[1]["needsCheck"] is True
    c2, j2 = fresh(agendas=(P1, A2), llm=lambda u: answer_pts([("decided", "새 문장", [(1, 2)]), ("undecided", "담당이 갈림", [(3, 3)]), ("none", "", [])]),
                   flag_conflict=False)
    feed(c2, L4, 7000)
    j2.process()
    j2.apply_edits([edit(1)])
    assert j2.board_status()["p1"]["points"][1]["needsCheck"] is False
    # 20-2) 근거 줄의 글이 바뀌면(가) 확인 필요. 한 번 켜지면 다시 고칠 때까지 유지
    c3, j3 = fresh(agendas=(P1, A2), llm=lambda u: ANS3(), flag_conflict=False)
    feed(c3, L4, 7000)
    j3.process()
    j3.apply_edits([edit(1)])
    assert j3.board_status()["p1"]["points"][1]["needsCheck"] is False
    feed(c3, [], 8000, edits=[{"id": 3, "rev": 1, "text": "배포 담당은 일단 보류해요", "text_marked": "배포 담당은 일단 보류해요"}])
    assert j3.board_status()["p1"]["points"][1]["needsCheck"] is True
    assert j3.board_status()["p1"]["points"][1]["needsCheck"] is True, "유지"
    j3.apply_edits([edit(2, text="담당은 보류")])
    assert j3.board_status()["p1"]["points"][1]["needsCheck"] is False, "다시 고치면 꺼진다"
    # 20-3) 근거 줄이 다른 안건으로 옮겨 가면(나) 확인 필요
    c4, j4 = fresh(agendas=(P1, A2), llm=lambda u: ANS3(), flag_conflict=False)
    feed(c4, L4, 7000)
    j4.process()
    j4.apply_edits([edit(1)])
    assert j4.board_status()["p1"]["points"][1]["needsCheck"] is False
    c4.locks[3] = {"key": "a2"}
    assert j4.board_status()["p1"]["points"][1]["needsCheck"] is True
    # 20-4) 사항 문구가 바뀌면 그 안건을 바로 다시 판정한다. 같은 목록을 다시 받으면 다시 판정하지 않는다
    c7, j7 = fresh(agendas=(P1, A2), llm=lambda u: ANS3())
    feed(c7, L4, 7000)
    j7.process()
    n = j7.calls
    P1b = agp("p1", "배포 일정", [(7, "1차 배포 날짜를 언제로 할지"), (8, "배포 담당 지정"), (9, "사항기밀 점검 항목 확정")], 1)
    c7.set_agendas(2, [P1b, A2])
    assert j7.process() == 1 and j7.calls == n + 1, "사항 문구가 바뀌면 기다리지 않고 다시 판정"
    c7.set_agendas(3, [P1b, A2])
    assert j7.process() == 0, "문구가 같으면 다시 판정하지 않는다"
    # 20-5) 모르는 사항의 고침도 보관했다가 목록에 생기면 적용하고, 목록에서 사라지면 버린다
    c5, j5 = fresh(agendas=(P1, A2), llm=lambda u: ANS3())
    feed(c5, L4, 7000)
    j5.process()
    assert j5.apply_edits([edit(1, pid=99, text="나중에 생긴 사항 결정")])["applied"] == 1
    assert [p["decisionPointId"] for p in j5.board_status()["p1"]["points"]] == [7, 8, 9], "목록에 없는 사항은 담지 않는다"
    P1c = agp("p1", "배포 일정", PTS + [(99, "추가된 사항")], 1)
    c5.set_agendas(2, [P1c, A2])
    plan = [answer_pts([("decided", "a", [(1, 2)]), ("check", "b", [(3, 3)]), ("undecided", "c", [(4, 4)]), ("none", "", [])])]
    j5.llm = lambda u: plan.pop(0)
    j5.process()
    q = j5.board_status()["p1"]["points"][3]
    assert q["decisionPointId"] == 99 and q["edited"] and q["decisionText"] == "나중에 생긴 사항 결정", q
    c5.set_agendas(3, [P1, A2])
    j5.process()
    assert ("p1", "99") not in j5.edits and [p["decisionPointId"] for p in j5.board_status()["p1"]["points"]] == [7, 8, 9]
    # 20-6) 줄이 없는 안건의 사항도 고칠 수 있다 (고친 글을 담고 상태는 논의 전). 안건이 사라지면 고친 내용도 버린다
    c6, j6 = fresh(agendas=(P1, A2), llm=lambda u: answer("none"))
    j6.apply_edits([edit(1, ev=(), pid=7, text="회의 전에 미리 적은 결정")])
    bs = j6.board_status()["p1"]
    assert bs["status"] == NOT_STARTED and bs["reason"] is None
    assert bs["points"][0] == {"decisionPointId": 7, "status": NOT_STARTED, "decisionText": "회의 전에 미리 적은 결정",
                               "evidenceSeqs": [], "edited": True, "needsCheck": False}, bs["points"][0]
    j6.process()
    c6.set_agendas(2, [A2, A3])
    j6.process()
    assert ("p1", "7") not in j6.edits
    # 20-7) 안건 목록의 id 가 숫자여도 돌려주는 사항 번호는 받은 값이다. 사항 번호가 없는 옛 모양은 번호 null 의 사항 1개
    cx, jx = fresh(llm=lambda u: answer("none"))
    assert jx.board_status()["a1"]["points"][0]["decisionPointId"] is None and len(jx.board_status()["a1"]["points"]) == 1
    assert agenda_points(A1) == [{"id": None, "key": "@1", "position": 1, "text": "배포 날짜를 확정한다"}]
    # 20-8) info 는 사항 개수만 센다
    info = j3.info()
    assert info["decided"] == 1 and info["decisionCheck"] == 1


    # 18) 로그에는 전사, 안건 제목, 판정 이유가 남지 않는다
    logging.getLogger("ai-service").removeHandler(h)
    text = "\n".join(records)
    for secret in ("기밀단어", "배포 일정", "배포는 다음 주 목요일로 정함", "목요일", "사항기밀", "인프라 팀", "정해지지 않음"):
        assert secret not in text, f"로그에 내용이 남음: {secret}"
    assert records
    print("agenda_status selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
