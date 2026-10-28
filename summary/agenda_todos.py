#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 할 일 추출 (AI-05 중 할 일)

발언에서 "누가 무엇을 언제까지 하겠다"를 뽑아 안건 보드의 안건 안(agendas[].todos[])에 담는다.
형식은 docs/api/agenda-opinions-contract.md, 예시는 examples/api/. 입력 구성, 갱신 시점, 검증, 사람이 고친 항목 잠금은
agenda_items.py 의 공통 뼈대를 쓰고, 이 파일은 프롬프트, 항목 모양, 담당자 이름 맞추기를 정한다.

항목
  id, task, assigneeName(발언에 나온 이름 그대로, 없으면 null), assigneeStatus, assigneeUserId, dueText(말한 그대로, 없으면 null),
  evidenceSeqs, edited, needsCheck, unclear(할 일인지 불분명하면 true)

담당자 이름 맞추기 (모델이 아니라 코드가 한다. 모델에는 참석자 명단을 주지 않는다)
  assigneeStatus  MATCHED 명단과 맞음 / UNASSIGNED 담당자 미정 / NOT_IN_ROSTER 명단에 없는 담당자 / AMBIGUOUS 맞는 사람이 둘 이상 /
                  null 이름은 있으나 명단을 받지 못해 비교하지 못함
  명단이 바뀌면 모델을 부르지 않고 보드를 만들 때 다시 맞춘다.

규칙
  - 기한은 날짜로 바꾸지 않고 말한 그대로 둔다 (상대 표현을 날짜로 바꾸면 틀리기 쉽고, 틀린 날짜가 더 위험하다).
  - assigneeName 과 dueText 는 근거 줄의 글에 그대로 있어야 한다. 없으면 그 값을 null 로 바꾸고 unclear 를 켠다 (모델이 지어낸 값을 막는다).
  - 화자를 구분하지 않으므로 "제가 할게요"는 이름이 없다 (UNASSIGNED).
  - 안건에 연결된 줄이 1개 이상이면 호출한다 (한 줄로도 할 일이 생긴다). 호출 전에 신호 단어로 거르지 않는다.

모델 호출은 task="agenda_todos" 로 토큰·호출 시간 지표에 남긴다. LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import json
import logging
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from agenda_items import AgendaItemExtractor, ItemCfg, unquote_trivial  # noqa: E402
from meeting_summary import build_char_stream, squash  # noqa: E402

TASK = "agenda_todos"
MATCHED, UNASSIGNED, NOT_IN_ROSTER, AMBIGUOUS = "MATCHED", "UNASSIGNED", "NOT_IN_ROSTER", "AMBIGUOUS"

SYSTEM_PROMPT = """너는 회의에서 한 안건에 나온 할 일을 뽑는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어. [맞장구]가 붙은 줄은 짧은 호응이야.

규칙
1. 전사는 회의 전체가 아니라 하나의 안건에 연결된 발언만 모은 거야. 근거 줄 번호는 이 전사의 번호만 써.
2. [안건]의 제목과 결정할 사항은 이야기가 무엇에 관한 것인지 이해하는 데만 써. 전사에 없는 내용을 제목에서 가져와 쓰지 마.
3. 누가 무엇을 하겠다, 하자, 해 달라고 말한 발언에서 할 일을 뽑아. 말하는 사람이 스스로 맡겠다고 한 말("~해 줄게요", "~하겠습니다")과 상대에게 부탁한 말("~해 주세요")도 모두 포함해. 단순한 의견, 질문, 이미 끝난 일의 보고는 할 일이 아니야.
   3-1. 회의에 없는 외부 사람(거래처, 다른 부서, 담당 공무원 등)이 하기로 한 일이나 그 사람에게 맡긴 일도 할 일이야. 회의 참석자만 고르지 마. assigneeName에는 전사에 나온 그 사람의 이름이나 호칭을 그대로 써.
   3-2. 담당이 정해지지 않았어도 "~해야 해요", "~해야 합니다"처럼 구체적인 행동을 해야 한다고 말했고 아무도 반대하지 않았으면 할 일로 뽑아. 이때 assigneeName은 null이야. 구체적인 행동이 없는 일반적인 바람이나 의견("잘하면 좋겠다")은 뽑지 마.
   3-3. 보류하거나 취소하기로 한 일은 뽑지 마. 보류 뒤에 다시 하기로 했으면 다시 하기로 한 내용만 뽑아.
4. task는 "~하기"처럼 해야 할 일을 짧은 한 문장으로 네 말로 정리해서 써. 말을 통째로 옮기지 마. 전사에 있는 내용만 쓰고, 전사에 없는 사실, 이름, 숫자를 쓰지 마. 숫자(금액, 날짜, 시간처럼 값이 있는 말)나 고유한 용어만 「」로 묶고 입력 원문과 똑같이 써(⟨ ⟩ 기호는 빼). 사람 이름, 호칭, "한 명"처럼 단위 앞의 한 글자 말은 「」로 묶지 말고 그냥 써. ⟨ ⟩로 묶인 글자는 STT가 확신하지 못한 부분이야. 그 부분을 근거로 쓰면 task에 "STT 불확실"이라고 밝혀. 숫자는 말한 한글 표기를 「」로 그대로 인용해.
5. assigneeName은 전사에 나온 이름을 글자 그대로 써. "님", "씨"는 빼도 돼. 이름이 전사에 없으면 null이야. "제가", "저는", "우리가"처럼 누구인지 알 수 없는 말은 이름이 아니야. 이름을 추측하거나 지어내지 마.
6. dueText는 기한을 전사에 나온 말 그대로("수요일까지") 써. 날짜로 바꾸지 마. 기한이 없으면 null이야.
7. unclear는 할 일인지 분명하지 않은 발언일 때 true야(예: "누가 좀 알아봐요"). 분명하면 false야.
8. 같은 사람의 같은 일이 반복되면 하나로 합쳐.
9. 모든 항목에 근거 줄 번호 구간 [[첫 번호, 끝 번호], ...]을 달아. 구간의 양 끝은 입력에 있는 번호여야 해. 할 일을 말한 줄과, 이름이나 기한이 나온 줄만 가리켜.
10. [사람이 확정한 항목]이 있으면 그 내용은 다시 쓰지 마.
11. 할 일이 없으면 todos를 빈 목록으로 줘.

출력은 JSON만. 형식:
{"todos": [{"task": "...", "assigneeName": "이름 또는 null", "dueText": "기한 또는 null", "unclear": false, "evidence": [[첫 번호, 끝 번호]]}]}"""


class TodoCfg(ItemCfg):
    """할 일 추출 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""
    MIN_NEW_LINES = 4                 # 평가(회의 A·B)를 이 값으로 했다
    MIN_LINES = 1                     # 한 줄로도 할 일이 생긴다
    MAX_ITEMS = 20                    # 안건당 항목 수 (비정상적으로 늘어나는 것을 막는 상한)


# --------------------------------------------------------------------------------------
# 담당자 이름 맞추기
# --------------------------------------------------------------------------------------
def _core(name):
    """비교용 이름: 공백을 빼고 끝의 호칭(님, 씨)을 뗀다."""
    s = squash(str(name))
    if len(s) >= 3 and s[-1] in "님씨":
        s = s[:-1]
    return s


def match_assignee(name, roster):
    """발언에 나온 이름을 참석자 명단과 맞춘다. 반환: (assigneeStatus, userId 또는 None).
    roster: [{"user_id", "name"}]. 규칙은 계약 6.3장과 같다."""
    if name is None or not str(name).strip():
        return UNASSIGNED, None
    if not roster:
        return None, None                                    # 명단을 받지 못해 비교하지 못함
    n = _core(name).lower()
    if not n:
        return NOT_IN_ROSTER, None
    exact = [p for p in roster if _core(p["name"]).lower() == n]
    if exact:
        return (MATCHED, exact[0]["user_id"]) if len(exact) == 1 else (AMBIGUOUS, None)
    if len(n) >= 2:                                          # 명단 이름의 첫 글자를 성으로 보고 뗀 이름이 같은 사람
        given = [p for p in roster if _core(p["name"]).lower()[1:] == n]
        if given:
            return (MATCHED, given[0]["user_id"]) if len(given) == 1 else (AMBIGUOUS, None)
    return NOT_IN_ROSTER, None


# --------------------------------------------------------------------------------------
# 모의 모델 (모델을 부르지 않는다. 비용 0)
# --------------------------------------------------------------------------------------
_ROW = re.compile(r"^\[(\d+)\] (\d\d:\d\d) (\[맞장구\] )?(.*)$", re.M)
_NAMES = ("철수", "영희", "민수", "영수", "지민")
_DUE = re.compile(r"((?:이번 주 |다음 주 )?(?:월요일|화요일|수요일|목요일|금요일|오늘|내일|모레)까지)")
_WORK = ("할게요", "하겠습니다", "맡을게요", "해 주세요", "해주세요")


def echo_llm(user):
    """줄의 말투로 할 일을 만드는 모의 모델. 구조(JSON 형식, 근거 번호)만 맞춘다.
    '할게요·하겠습니다·맡을게요·해 주세요' 가 있으면 할 일, '누가 좀' 이면 불분명한 할 일. 이름은 정해 둔 몇 개만 알아본다."""
    body = user.split("[전사]\n", 1)[-1]
    out = []
    for n, _t, bc, text in _ROW.findall(body):
        if bc:
            continue
        text = re.sub(r"[⟨⟩]", "", text)
        vague = "누가 좀" in text
        if not (vague or any(w in text for w in _WORK)):
            continue
        name = next((x for x in _NAMES if x in text), None)
        m = _DUE.search(text)
        out.append({"task": f"{n}번 줄의 할 일", "assigneeName": name, "dueText": m.group(1) if m else None,
                    "unclear": vague, "evidence": [[int(n), int(n)]]})
    return json.dumps({"todos": out}, ensure_ascii=False)


class AgendaTodos(AgendaItemExtractor):
    TASK = TASK
    TARGET = "todo"
    LETTER = "t"
    ROOT = "todos"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    echo = staticmethod(echo_llm)

    def __init__(self, classifier, cfg, llm=None, id_prefix=None, now=time.time):
        super().__init__(classifier, cfg, llm=llm, id_prefix=id_prefix, now=now)
        self.roster = []              # [{"user_id", "name"}]
        self.scrubbed = 0             # 근거 줄에 없어서 null 로 바꾼 이름·기한 수

    def set_roster(self, attendees):
        """참석자 명단을 통째로 바꾼다. userId 가 겹치면 먼저 온 항목만 쓴다."""
        out, seen = [], set()
        for p in attendees or []:
            k = str(p["user_id"])
            if k in seen:
                continue
            seen.add(k)
            out.append({"user_id": p["user_id"], "name": p["name"]})
        with self.lock:
            self.roster = out
        self.log.info("참석자 명단 반영 meeting=%s attendees=%s", self.c.meeting_id, len(out))

    def _intro(self, n_lines, need):
        return (f"다음은 하나의 안건에 연결된 회의 발언이야 (줄 {n_lines}개). [맞장구]가 붙은 줄은 짧은 호응이야. "
                f"항목은 최대 {need}개까지 만들어줘.")

    def _candidate(self, d):
        d = d if isinstance(d, dict) else {}
        return {"text": d.get("task"), "topic": None, "evidence": d.get("evidence")}

    def _check(self, d, pool):
        name, due, unclear = d.get("assigneeName"), d.get("dueText"), d.get("unclear", False)
        errs = []
        if name is not None and not isinstance(name, str):
            errs.append("assigneeName은 문자열이나 null이어야 함")
        if due is not None and not isinstance(due, str):
            errs.append("dueText는 문자열이나 null이어야 함")
        if unclear is None:
            unclear = False
        if not isinstance(unclear, bool):
            errs.append("unclear는 true나 false여야 함")
        if errs:
            return errs, None
        name = (name or "").strip() or None
        due = (due or "").strip() or None
        stream, _mask = build_char_stream(pool)
        if name is not None and _core(name) not in stream:       # 근거 줄에 없는 이름은 지어낸 것으로 본다
            name, unclear = None, True
            self.scrubbed += 1
        if due is not None and squash(due) not in stream:
            due, unclear = None, True
            self.scrubbed += 1
        task = unquote_trivial(d["task"].strip(), [name] + [p["name"] for p in self.roster])
        return [], {"task": task, "assignee_name": name, "assignee_user_id": None, "due_text": due, "unclear": unclear}

    def _edit_data(self, e):
        name = (e.get("assignee_name") or "").strip() or None
        due = (e.get("due_text") or "").strip() or None
        return {"task": e["task"], "assignee_name": name, "assignee_user_id": e.get("assignee_user_id"), "due_text": due,
                "unclear": False}

    def _locked_text(self, it):
        d = it["data"]
        out = d["task"]
        if d["assignee_name"]:
            out += f" / 담당: {d['assignee_name']}"
        if d["due_text"]:
            out += f" / 기한: {d['due_text']}"
        return out

    def _resolve(self, d):
        """항목의 (assigneeStatus, assigneeUserId). 사용자가 명단에서 직접 고른 userId 가 명단에 있으면 그 사람이다."""
        uid = d.get("assignee_user_id")
        if uid is not None:
            for p in self.roster:
                if str(p["user_id"]) == str(uid):
                    return MATCHED, p["user_id"]
        return match_assignee(d["assignee_name"], self.roster)

    def _board_item(self, it):
        d = it["data"]
        status, uid = self._resolve(d)
        return {"id": it["id"], "task": d["task"], "assigneeName": d["assignee_name"], "assigneeStatus": status,
                "assigneeUserId": uid if status == MATCHED else None, "dueText": d["due_text"],
                "evidenceSeqs": list(it["evidence"]), "edited": bool(it["edited"]), "needsCheck": bool(it["needs_check"]),
                "unclear": bool(d["unclear"])}

    def _sort_items(self, items, by_id):
        inf = float("inf")
        return sorted(items, key=lambda it: (not it["edited"], min((by_id[s]["start_ms"] for s in it["evidence"] if s in by_id),
                                                                   default=inf)))

    def info(self):
        out = super().info()
        with self.lock:
            out["unclear"] = sum(1 for st in self.ag.values() for it in st["items"] if it["data"]["unclear"])
            out["scrubbed"] = self.scrubbed
            out["roster"] = len(self.roster)
        return out


# --------------------------------------------------------------------------------------
# 자체 시험 (모델을 부르지 않는다)
# --------------------------------------------------------------------------------------
def selftest():
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    # 1) 이름 맞추기
    R = [{"user_id": 7, "name": "김철수"}, {"user_id": 8, "name": "이영희"}, {"user_id": 9, "name": "박민수"},
         {"user_id": 10, "name": "최민수"}, {"user_id": 11, "name": "Alex Kim"}]
    assert match_assignee("김철수", R) == (MATCHED, 7), "정확히 같은 이름"
    assert match_assignee("철수", R) == (MATCHED, 7), "성을 뗀 이름"
    assert match_assignee("철수님", R) == (MATCHED, 7) and match_assignee("철수 씨", R) == (MATCHED, 7), "호칭"
    assert match_assignee("영희", R) == (MATCHED, 8)
    assert match_assignee("민수", R) == (AMBIGUOUS, None), "맞는 사람이 둘"
    assert match_assignee("박민수", R) == (MATCHED, 9), "정확히 같으면 동명이인이 아니다"
    assert match_assignee("영수", R) == (NOT_IN_ROSTER, None)
    assert match_assignee("철", R) == (NOT_IN_ROSTER, None), "한 글자는 성을 뗀 이름으로 보지 않는다"
    assert match_assignee("alex kim", R) == (MATCHED, 11), "영문은 대소문자와 공백을 무시"
    assert match_assignee(None, R) == (UNASSIGNED, None) and match_assignee("  ", R) == (UNASSIGNED, None)
    assert match_assignee("철수", []) == (None, None), "명단이 없으면 비교하지 않는다"
    assert match_assignee(None, []) == (UNASSIGNED, None), "이름이 없으면 명단과 상관없이 담당자 미정"
    assert match_assignee("씨", R) == (NOT_IN_ROSTER, None) or match_assignee("씨", R)[0] in (NOT_IN_ROSTER, None)
    dup = R + [{"user_id": 12, "name": "김철수"}]
    assert match_assignee("김철수", dup) == (AMBIGUOUS, None), "명단에 같은 이름이 둘"

    def ln(i, start, text, kind=None, low=False):
        return {"id": i, "start_ms": start, "end_ms": start + 1000, "arrival_ms": start + 1000, "type": "final",
                "kind": kind, "low_confidence": low, "text": text, "text_marked": text, "manual": False}

    def ag(i, title, decision, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": decision, "added": False}

    A1 = ag("a1", "배포 일정", "배포 날짜를 확정한다", 1)
    A2 = ag("a2", "예산 배분", "비용 한도를 정한다", 2)
    L1 = ln(1, 0, "배포 철수가 QA 일정표 수요일까지 공유할게요")
    L2 = ln(2, 4000, "배포 체크리스트는 제가 할게요")
    L3 = ln(3, 8000, "배포 영수가 사용량 확인하겠습니다")
    L4 = ln(4, 12000, "배포 누가 좀 알아봐요")
    L5 = ln(5, 16000, "배포 일정은 어떻게 생각해요")

    def fresh(agendas=(A1, A2), llm=None, roster=None, **kw):
        c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
        if agendas:
            c.set_agendas(1, list(agendas))
        kw.setdefault("min_new_lines", 2)                 # 시험은 새 줄 2개 기준으로 짜여 있다 (서비스 기본값은 4)
        t = AgendaTodos(c, TodoCfg(**kw), llm=llm, id_prefix="p")
        if roster is not None:
            t.set_roster(roster)
        return c, t

    def feed(c, lines, sent_ms, edits=()):
        c.receive({"lines": lines, "edits": list(edits), "sent_ms": sent_ms})
        c.process()

    def items(t, k="a1"):
        return t.board_items()[k]

    records = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    h = Grab()
    logging.getLogger("ai-service").addHandler(h)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)
    ROSTER = [{"user_id": 7, "name": "김철수"}, {"user_id": 8, "name": "이영희"}]

    def ans(*rows):
        return json.dumps({"todos": [{"task": t, "assigneeName": n, "dueText": d, "unclear": u, "evidence": ev}
                                     for t, n, d, u, ev in rows]}, ensure_ascii=False)

    # 1-2) 이름·단위 글자에 잘못 붙은 낫표는 뗀다. 값이 있는 숫자 인용은 그대로 둔다
    assert unquote_trivial("답사 때 「박」 주무관에게 묻기") == "답사 때 박 주무관에게 묻기"
    assert unquote_trivial("요원 「한」 명 더 두기") == "요원 한 명 더 두기"
    assert unquote_trivial("「김철수」가 공유하기", ["김철수"]) == "김철수가 공유하기"
    assert unquote_trivial("예산 「삼십 오만」 원 확정하기") == "예산 「삼십 오만」 원 확정하기"
    assert "외부 사람" in SYSTEM_PROMPT and "해야 해요" in SYSTEM_PROMPT and "보류" in SYSTEM_PROMPT

    # 2) 줄이 없으면 모델을 부르지 않는다. 줄이 1개라도 호출 대상이다 (60초 기다린 뒤)
    c, t = fresh()
    assert t.process() == 0 and t.calls == 0 and all(v == [] for v in t.board_items().values())
    feed(c, [L2], 5000)
    assert t.process() == 0
    c.advance(30000)
    assert t.process() == 0
    c.advance(80000)
    assert t.process() == 1, "새 줄 1개: 60초 뒤 호출 (줄 수 조건은 없다)"

    # 3) 새 줄 2개 이상이면 바로. 항목 모양, 시간 순서, 근거 줄. 이름 맞추기는 명단이 있어야 한다
    seen = []
    c, t = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, [L1, L2, L3, L4, L5], 17000)
    assert t.process() == 1 and len(seen) == 1
    assert "제목: 배포 일정" in seen[0] and "[1] 00:00" in seen[0] and "최대 20개" in seen[0], seen[0]
    b = items(t)
    assert [x["evidenceSeqs"] for x in b] == [[1], [2], [3], [4]], b
    assert set(b[0]) == {"id", "task", "assigneeName", "assigneeStatus", "assigneeUserId", "dueText", "evidenceSeqs", "edited",
                         "needsCheck", "unclear"}
    assert [(x["assigneeName"], x["dueText"], x["unclear"]) for x in b] == [
        ("철수", "수요일까지", False), (None, None, False), ("영수", None, False), (None, None, True)], b
    assert [x["assigneeStatus"] for x in b] == [None, UNASSIGNED, None, UNASSIGNED], "명단이 없으면 이름이 있는 항목은 비교하지 않음"
    assert all(x["assigneeUserId"] is None and x["edited"] is False and x["needsCheck"] is False for x in b)
    assert len({x["id"] for x in b}) == 4 and all(x["id"].startswith("p-t") for x in b)
    assert t.process() == 0
    json.dumps(t.board_items())
    # 4) 명단이 바뀌면 모델을 부르지 않고 이름을 다시 맞춘다
    n = t.calls
    t.set_roster(ROSTER)
    b = items(t)
    assert [(x["assigneeStatus"], x["assigneeUserId"]) for x in b] == [(MATCHED, 7), (UNASSIGNED, None), (NOT_IN_ROSTER, None),
                                                                    (UNASSIGNED, None)], b
    assert t.calls == n and t.process() == 0
    t.set_roster([{"user_id": 7, "name": "김철수"}, {"user_id": 7, "name": "다른 사람"}, {"user_id": 3, "name": "박영수"}])
    assert len(t.roster) == 2 and [x["assigneeStatus"] for x in items(t)][2] == MATCHED, "userId 가 겹치면 먼저 온 항목만 쓴다"
    t.set_roster([])
    assert items(t)[0]["assigneeStatus"] is None

    # 5) 맞장구는 앞 줄의 안건에 붙이고, 간투사·낮은 확신도 줄은 뺀다
    seen = []
    c, t = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, [L1, ln(2, 2500, "네 좋아요", kind="backchannel"), ln(3, 4000, "배포 일정 확정해요"), ln(4, 5500, "음", kind="filler"),
             ln(5, 6000, "배포 흐릿한 말 할게요", low=True)], 7000)
    t.process()
    assert "[맞장구] 네 좋아요" in seen[-1] and "흐릿한" not in seen[-1], seen[-1]
    n = t.calls
    feed(c, [ln(6, 8000, "네 그렇죠", kind="backchannel")], 9000)
    assert t.process() == 0 and t.calls == n, "맞장구만 늘어남"

    # 6) 이름·기한이 근거 줄에 없으면 null 로 바꾸고 unclear 를 켠다 (항목은 남긴다)
    mk = ans(("QA 일정표 공유", "민지", "금요일까지", False, [[1, 1]]))
    c, t = fresh(llm=lambda u: mk, roster=ROSTER)
    feed(c, [L1, L2], 5000)
    t.process()
    b = items(t)
    assert len(b) == 1 and (b[0]["assigneeName"], b[0]["dueText"], b[0]["unclear"], b[0]["assigneeStatus"]) == (None, None, True, UNASSIGNED), b
    assert t.scrubbed == 2 and t.rejected == 0
    ok = ans(("QA 일정표 공유", "철수님", "수요일까지", False, [[1, 1]]))
    c, t = fresh(llm=lambda u: ok, roster=ROSTER)
    feed(c, [L1, L2], 5000)
    t.process()
    b = items(t)
    assert (b[0]["assigneeName"], b[0]["dueText"], b[0]["unclear"], b[0]["assigneeStatus"], b[0]["assigneeUserId"]) == \
        ("철수님", "수요일까지", False, MATCHED, 7), b

    # 7) 검증: 근거 번호가 틀리면 오류를 알려 한 번 더. 그래도 틀리면 그 항목은 내지 않는다 (연속 실패로 세지 않음)
    inputs = []
    answers = [ans(("할 일", None, None, False, [[9, 9]])), ans(("QA 일정표 공유", "철수", None, False, [[1, 1]]))]
    c, t = fresh(llm=lambda u: inputs.append(u) or answers.pop(0))
    feed(c, [L1, L2], 5000)
    assert t.process() == 2 and "[검증 오류]" in inputs[1] and [x["evidenceSeqs"] for x in items(t)] == [[1]] and t.rejected == 0
    bad = ans(("할 일", None, None, False, [[9, 9]]))
    answers = [bad, bad]
    c, t = fresh(llm=lambda u: answers.pop(0))
    feed(c, [L1, L2], 5000)
    assert t.process() == 2 and items(t) == [] and t.rejected == 1 and t.err_streak == 0 and not t.halted
    # 8) 타입이 틀린 칸(assigneeName 이 숫자 등)도 같다
    wrong = json.dumps({"todos": [{"task": "할 일", "assigneeName": 5, "dueText": None, "unclear": False, "evidence": [[1, 1]]}]})
    answers = [wrong, wrong]
    inputs = []
    c, t = fresh(llm=lambda u: inputs.append(u) or answers.pop(0))
    feed(c, [L1, L2], 5000)
    assert t.process() == 2 and "assigneeName" in inputs[1] and items(t) == [] and t.rejected == 1
    answers = ["JSON 아님", json.dumps({"todos": "x"})]
    c, t = fresh(llm=lambda u: answers.pop(0))
    feed(c, [L1, L2], 5000)
    assert t.process() == 2 and t.errors == 1 and items(t) == [] and not t.halted

    # 9) 호출이 연속 3번 실패하면 이 기능만 멈춘다. 호출 상한
    def boom(u):
        raise RuntimeError("키 오류 기밀단어")
    c, t = fresh(llm=boom, min_new_lines=1)
    feed(c, [L1], 1000)
    for k in range(5):
        feed(c, [ln(10 + k, 2000 + k * 1000, "배포 계속 할게요")], 3000 + k * 1000)
        t.process()
    assert t.halted == "연속 실패" and t.calls == 3 and t.errors == 3, (t.halted, t.calls)
    c, t = fresh(llm=echo_llm, max_calls=1)
    feed(c, [L1, L2, ln(3, 8000, "비용 얘기 할게요"), ln(4, 9000, "비용 계속 할게요")], 10000)
    assert t.process() == 1 and t.halted == "호출 상한"

    # 10) 사람이 고친 할 일: 고정, 모델에는 "이미 있는 항목"으로. 이름은 명단과 다시 맞춘다
    seen = []
    c, t = fresh(llm=lambda u: seen.append(u) or echo_llm(u), roster=ROSTER + [{"user_id": 9, "name": "박민수"}])
    feed(c, [L1, L2], 5000)
    t.process()
    r = t.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "task": "QA 일정표 공유", "assignee_name": "철수",
                        "assignee_user_id": None, "due_text": "수요일까지", "evidence_seqs": [1], "rev": 3}])
    assert r == {"applied": 1, "ignored": []}, r
    r = t.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "task": "낡은 글", "assignee_name": None,
                        "assignee_user_id": None, "due_text": None, "evidence_seqs": [1], "rev": 3},
                       {"agenda_id": "a1", "item_id": "nope", "action": "confirm", "rev": 1}])
    assert r == {"applied": 0, "ignored": [{"itemId": "old-1", "reason": "stale"}, {"itemId": "nope", "reason": "not_edited"}]}, r
    assert t.process() == 1, "고친 안건은 기다리지 않고 다시 처리"
    assert "- (근거 줄 1~1) QA 일정표 공유 / 담당: 철수 / 기한: 수요일까지" in seen[-1], seen[-1]
    b = items(t)
    assert b[0] == {"id": "old-1", "task": "QA 일정표 공유", "assigneeName": "철수", "assigneeStatus": MATCHED, "assigneeUserId": 7,
                    "dueText": "수요일까지", "evidenceSeqs": [1], "edited": True, "needsCheck": False, "unclear": False}, b
    assert [x["edited"] for x in b] == [True, False, False], "고친 항목이 맨 앞, AI 항목이 뒤따름 (모의 모델은 같은 내용을 걸러 내지 않는다)"
    # 사용자가 명단에서 직접 고른 사람, 명단에 없는 이름, 이름을 비움
    for rev, name, uid, want in ((4, "민수", 9, (MATCHED, 9)), (5, "영수", None, (NOT_IN_ROSTER, None)), (6, None, None, (UNASSIGNED, None)),
                                 (7, "철수", 99, (MATCHED, 7)), (8, "민수", None, (MATCHED, 9))):
        t.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "task": "QA 일정표 공유", "assignee_name": name,
                        "assignee_user_id": uid, "due_text": None, "evidence_seqs": [1], "rev": rev}])
        got = [(x["assigneeStatus"], x["assigneeUserId"]) for x in items(t) if x["id"] == "old-1"][0]
        assert got == want, (rev, name, uid, got)
    # 11) needsCheck: 근거 줄의 글이 바뀌면 켜진다. confirm 으로 꺼진다
    t.process()
    feed(c, [], 6000, edits=[{"id": 1, "rev": 1, "text": "배포 철수가 금요일까지 할게요", "text_marked": "배포 철수가 금요일까지 할게요"}])
    assert [x["needsCheck"] for x in items(t) if x["id"] == "old-1"] == [True]
    assert t.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "confirm", "rev": 9}])["applied"] == 1
    t.process()
    assert [x["needsCheck"] for x in items(t) if x["id"] == "old-1"] == [False]

    # 12) 줄이 다른 안건으로 옮겨 가면 기다리지 않고 다시 처리한다. 줄이 모두 사라지면 AI 항목을 비운다 (고친 항목은 남긴다)
    c, t = fresh(llm=echo_llm)
    feed(c, [L1, L2, L3], 9000)
    t.process()
    n = t.calls
    c.apply_corrections([{"seq": 3, "agenda_id": "a2", "rev": 1}])
    assert t.process() == 2 and t.calls == n + 2, "줄이 빠진 안건과 받은 안건 모두 바로 다시 처리"
    assert all(3 not in x["evidenceSeqs"] for x in items(t)) and [x["evidenceSeqs"] for x in items(t, "a2")] == [[3]]
    t.apply_edits([{"agenda_id": "a2", "item_id": "k-1", "action": "edit", "task": "고친 할 일", "assignee_name": None,
                    "assignee_user_id": None, "due_text": None, "evidence_seqs": [3], "rev": 1}])
    feed(c, [], 10000, edits=[{"id": 3, "rev": 2, "deleted": True}])
    t.process()
    assert [x["id"] for x in items(t, "a2")] == ["k-1"] and items(t, "a2")[0]["needsCheck"] is True

    # 13) 안건당 항목 수 상한
    c, t = fresh(llm=echo_llm, max_items=2)
    feed(c, [L1, L2, L3, L4], 13000)
    t.process()
    assert len(items(t)) == 2

    # 14) final 에는 기다리던 줄도 처리한다
    c, t = fresh(llm=echo_llm)
    feed(c, [L1], 1000)
    assert t.process() == 0 and t.process(final=True) == 1 and len(items(t)) == 1

    # 14-1) 실수 주입 시험으로 찾은 빈틈을 막는 시험
    assert match_assignee("수", [{"user_id": 1, "name": "이수"}]) == (NOT_IN_ROSTER, None), "한 글자 이름은 성을 뗀 이름과 비교하지 않는다"
    only_name = ans(("QA 일정표 공유", "민지", None, False, [[1, 1]]))               # 이름만 근거에 없다: 기한은 없던 값
    c, t = fresh(llm=lambda u: only_name, roster=ROSTER)
    feed(c, [L1, L2], 5000)
    t.process()
    b = items(t)
    assert (b[0]["assigneeName"], b[0]["unclear"]) == (None, True) and t.scrubbed == 1, b
    c, t = fresh(llm=echo_llm)                                                      # AI 항목은 근거 줄이 지워져도 needsCheck 를 켜지 않는다
    feed(c, [L1, L2, L3], 9000)
    t.process()
    assert items(t)
    feed(c, [], 10000, edits=[{"id": 1, "rev": 1, "deleted": True}])
    assert all(x["needsCheck"] is False for x in items(t)) and all(x["edited"] is False for x in items(t))
    seen = []                                                                       # 낮은 확신도 줄은 사람이 안건을 고쳐 정해도 입력에 넣지 않는다
    c, t = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, [L1, ln(2, 2500, "배포 흐릿한 말 할게요", low=True)], 5000)
    c.apply_corrections([{"seq": 2, "agenda_id": "a1", "rev": 1}])
    t.process(final=True)
    assert seen and all("흐릿한" not in u for u in seen), seen
    c, t = fresh(llm=echo_llm)                                                      # 맞장구만 늘어나면 오래 기다려도 호출하지 않는다
    feed(c, [L1, L2], 5000)
    t.process()
    n = t.calls
    feed(c, [ln(9, 6500, "네 그렇죠", kind="backchannel")], 7000)
    t.process()
    feed(c, [], 200000)
    assert t.process() == 0 and t.calls == n, "맞장구만 늘어남"

    # 15) 프롬프트, 설정, info
    for w in ("assigneeName", "dueText", "unclear", "날짜로 바꾸지 마", "지어내지 마", "사람이 확정한 항목", "JSON만"):
        assert w in SYSTEM_PROMPT, w
    d = TodoCfg()
    assert (d.min_lines, d.max_items, d.refresh_sec, d.min_new_lines, d.max_calls) == (1, 20, 60, 4, 300)
    try:
        TodoCfg(nope=1)
        assert False
    except TypeError:
        pass
    c, t = fresh(llm=echo_llm, roster=ROSTER)
    feed(c, [L1, L2, L4], 13000)
    t.process()
    info = t.info()
    assert set(info) == {"agendas", "items", "edited", "needsCheck", "calls", "errors", "rejected", "halted", "unclear", "scrubbed",
                         "roster"} and info["unclear"] == 1 and info["roster"] == 2, info

    # 16) 로그에는 전사, 이름, 할 일 문장이 남지 않는다
    logging.getLogger("ai-service").removeHandler(h)
    text = "\n".join(records)
    for secret in ("기밀단어", "철수", "영희", "QA 일정표", "배포 일정", "수요일까지", "고친 할 일"):
        assert secret not in text, f"로그에 내용이 남음: {secret}"
    assert records, "로그가 하나도 없음 (검사가 의미 없음)"
    print("agenda_todos selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
