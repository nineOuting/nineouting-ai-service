#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 의견·근거 정리 (AI-05 중 의견)

안건마다 나온 의견, 근거, 우려, 질문을 내용 기준으로 정리한다. 형식은 docs/api/agenda-opinions-contract.md, 예시는 examples/api/.
입력 구성, 갱신 시점, 검증, 사람이 고친 항목 잠금은 agenda_items.py 의 공통 뼈대를 쓰고, 이 파일은 프롬프트와 항목 모양만 정한다.

항목 (안건 보드 agendas[].opinions[])
  id, text(한 줄), kind(OPINION 의견 / EVIDENCE 근거 / CONCERN 우려 / QUESTION 질문), evidenceSeqs(근거 줄 번호, 1개 이상), edited, needsCheck

규칙
  - 발언자가 아니라 내용 기준이다 (화자 구분을 하지 않는다). 같은 의견의 반복은 하나로 합친다.
  - EVIDENCE(근거)는 전사에 실제로 나온 이유만 쓴다. 이유가 없는 의견에는 만들지 않는다 (AI가 근거를 만들어 넣지 않는다).
  - 안건에 연결된 줄이 min_lines(4)개 이상일 때부터 호출한다. 줄이 적으면 갈릴 의견이 없어 빈 호출이 된다.
  - 안건당 항목 수 상한 max_items(8). 사람이 고친 항목이 먼저 자리를 차지한다.

모델 호출은 task="agenda_opinions" 로 토큰·호출 시간 지표에 남긴다. LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import json
import logging
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from agenda_items import AgendaItemExtractor, ItemCfg  # noqa: E402

TASK = "agenda_opinions"
KINDS = ("OPINION", "EVIDENCE", "CONCERN", "QUESTION")

SYSTEM_PROMPT = """너는 회의에서 한 안건에 나온 의견을 정리하는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어. [맞장구]가 붙은 줄은 짧은 호응이야.

규칙
1. 전사는 회의 전체가 아니라 하나의 안건에 연결된 발언만 모은 거야. 근거 줄 번호는 이 전사의 번호만 써.
2. [안건]의 제목과 결정할 사항은 이야기가 무엇에 관한 것인지 이해하는 데만 써. 전사에 없는 내용을 제목에서 가져와 쓰지 마.
3. 누가 말했는지가 아니라 어떤 의견이 나왔는지를 내용 기준으로 정리해. 화자 구분은 없으니 사람 이름을 지어내지 마. 같은 의견이 반복되면 하나로 합쳐.
4. kind는 넷 중 하나야.
   - "OPINION": 어떤 안이나 방향에 대한 의견, 제안.
   - "EVIDENCE": 앞의 의견의 이유나 근거로 전사에 나온 사실과 설명.
   - "CONCERN": 우려, 걱정, 반대하는 이유.
   - "QUESTION": 아직 답이 나오지 않은 질문.
5. EVIDENCE는 전사에 실제로 나온 이유만 써. 이유가 전사에 없으면 만들지 마. 의견만 있으면 OPINION 항목만 만들어.
6. 같은 안(예: A안)에 대한 의견, 근거, 우려는 이어서 나열해.
7. text는 한 문장으로, 무슨 말이 나왔는지를 써. "~라는 의견이 나옴", "~라는 우려가 나옴"처럼 써. 결정이 났다고 쓰지 마(결정 여부는 따로 판정해).
8. 모든 항목에 근거 줄 번호 구간 [[첫 번호, 끝 번호], ...]을 달아. 구간의 양 끝은 입력에 있는 번호여야 해. 그 의견이 나온 줄만 가리켜.
9. text에는 근거 줄에 없는 사실, 이름, 숫자를 쓰지 마. 말을 인용할 때는 「」로 묶고 입력 원문과 똑같이 써(⟨ ⟩ 기호는 빼). ⟨ ⟩로 묶인 글자는 STT가 확신하지 못한 부분이야. 그 부분을 근거로 쓰면 text에 "STT 불확실"이라고 밝혀. 숫자는 말한 한글 표기를 「」로 그대로 인용해.
10. [사람이 확정한 항목]이 있으면 그 내용은 다시 쓰지 마.
11. 정리할 의견이 없으면 opinions를 빈 목록으로 줘.

출력은 JSON만. 형식:
{"opinions": [{"text": "...", "kind": "OPINION", "evidence": [[첫 번호, 끝 번호]]}]}"""


class OpinionCfg(ItemCfg):
    """의견 정리 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""
    MIN_LINES = 4                     # 안건 줄이 이만큼 이상일 때부터 호출
    MAX_ITEMS = 8                     # 안건당 항목 수


# --------------------------------------------------------------------------------------
# 모의 모델 (모델을 부르지 않는다. 비용 0)
# --------------------------------------------------------------------------------------
_ROW = re.compile(r"^\[(\d+)\] (\d\d:\d\d) (\[맞장구\] )?(.*)$", re.M)
_LABEL = {"OPINION": "의견", "EVIDENCE": "근거", "CONCERN": "우려", "QUESTION": "질문"}


def echo_llm(user):
    """줄의 말투로 종류를 정하는 모의 모델. 구조(JSON 형식, 근거 번호)만 맞춘다.
    '걱정·우려·부담·위험' 이면 CONCERN, '까요·어때요·?' 이면 QUESTION, '때문·이라서·니까' 이면 EVIDENCE,
    '하자·하죠·좋아요·찬성·제안' 이면 OPINION. 맞장구 줄은 건너뛴다."""
    body = user.split("[전사]\n", 1)[-1]
    out = []
    for n, _t, bc, text in _ROW.findall(body):
        if bc:
            continue
        text = re.sub(r"[⟨⟩]", "", text)
        if any(w in text for w in ("걱정", "우려", "부담", "위험")):
            kind = "CONCERN"
        elif any(w in text for w in ("까요", "어때요", "?")):
            kind = "QUESTION"
        elif any(w in text for w in ("때문", "이라서", "니까")):
            kind = "EVIDENCE"
        elif any(w in text for w in ("하자", "하죠", "좋아요", "찬성", "제안")):
            kind = "OPINION"
        else:
            continue
        out.append({"text": f"{n}번 줄에 {_LABEL[kind]} 표현이 나옴", "kind": kind, "evidence": [[int(n), int(n)]]})
    return json.dumps({"opinions": out}, ensure_ascii=False)


class AgendaOpinions(AgendaItemExtractor):
    TASK = TASK
    TARGET = "opinion"
    LETTER = "o"
    ROOT = "opinions"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    echo = staticmethod(echo_llm)

    def _intro(self, n_lines, need):
        return (f"다음은 하나의 안건에 연결된 회의 발언이야 (줄 {n_lines}개). [맞장구]가 붙은 줄은 짧은 호응이야. "
                f"항목은 최대 {need}개까지 만들어줘.")

    def _candidate(self, d):
        d = d if isinstance(d, dict) else {}
        return {"text": d.get("text"), "topic": None, "evidence": d.get("evidence")}

    def _check(self, d, pool):
        kind = d.get("kind")
        if kind not in KINDS:
            return ["kind는 OPINION, EVIDENCE, CONCERN, QUESTION 중 하나여야 함"], None
        return [], {"text": d["text"].strip(), "kind": kind}

    def _edit_data(self, e):
        return {"text": e["text"], "kind": e["kind"]}

    def _locked_text(self, it):
        return f"[{it['data']['kind']}] {it['data']['text']}"

    def _board_item(self, it):
        d = it["data"]
        return {"id": it["id"], "text": d["text"], "kind": d["kind"], "evidenceSeqs": list(it["evidence"]),
                "edited": bool(it["edited"]), "needsCheck": bool(it["needs_check"])}


# --------------------------------------------------------------------------------------
# 자체 시험 (모델을 부르지 않는다)
# --------------------------------------------------------------------------------------
def selftest():
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    def ln(i, start, text, kind=None, low=False):
        return {"id": i, "start_ms": start, "end_ms": start + 1000, "arrival_ms": start + 1000, "type": "final",
                "kind": kind, "low_confidence": low, "text": text, "text_marked": text, "manual": False}

    def ag(i, title, decision, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": decision, "added": False}

    A1 = ag("a1", "배포 일정", "배포 날짜를 확정한다", 1)
    A2 = ag("a2", "예산 배분", "비용 한도를 정한다", 2)
    BASE = [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 4000, "배포 QA 일정 때문에 어려워요"),
            ln(3, 8000, "배포 목요일은 부담이에요"), ln(4, 12000, "배포 금요일은 어때요")]

    def fresh(agendas=(A1, A2), llm=None, **kw):
        c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
        if agendas:
            c.set_agendas(1, list(agendas))
        return c, AgendaOpinions(c, OpinionCfg(**kw), llm=llm, id_prefix="p")

    def feed(c, lines, sent_ms, edits=()):
        c.receive({"lines": lines, "edits": list(edits), "sent_ms": sent_ms})
        c.process()

    def items(o, k="a1"):
        return o.board_items()[k]

    records = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    h = Grab()
    logging.getLogger("ai-service").addHandler(h)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)

    def ans(*rows):
        return json.dumps({"opinions": [{"text": t, "kind": k, "evidence": ev} for t, k, ev in rows]}, ensure_ascii=False)

    # 1) 줄이 4개 미만이면 모델을 부르지 않고 빈 목록
    c, o = fresh()
    feed(c, BASE[:3], 9000)
    assert o.process() == 0 and all(v == [] for v in o.board_items().values()) and o.calls == 0

    # 2) 줄이 4개가 되면 그 안건만 호출. 종류, 근거 줄, 항목 모양
    seen = []
    c, o = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, BASE, 13000)
    assert o.process() == 1 and len(seen) == 1
    u = seen[0]
    assert "제목: 배포 일정" in u and "결정할 사항: 배포 날짜를 확정한다" in u and "[1] 00:00" in u and "최대 8개" in u, u
    b = items(o)
    assert [x["kind"] for x in b] == ["OPINION", "EVIDENCE", "CONCERN", "QUESTION"], b
    assert [x["evidenceSeqs"] for x in b] == [[1], [2], [3], [4]]
    assert all(x["edited"] is False and x["needsCheck"] is False for x in b)
    assert set(b[0]) == {"id", "text", "kind", "evidenceSeqs", "edited", "needsCheck"}
    assert len({x["id"] for x in b}) == 4 and all(x["id"].startswith("p-o") for x in b)
    assert items(o, "a2") == [] and o.process() == 0, "같은 내용은 다시 부르지 않음"
    json.dumps(o.board_items())

    # 3) 새 줄 1개는 refresh_sec(음성 시간) 동안 기다린다. 2개 이상이면 바로
    n = o.calls
    feed(c, [ln(5, 16000, "배포 다시 하죠")], 17000)
    assert o.process() == 0 and o.calls == n
    c.advance(30000)
    assert o.process() == 0
    c.advance(80000)
    assert o.process() == 1, "60초가 지나면 호출"
    feed(c, [ln(6, 120000, "배포 서버가 걱정돼요"), ln(7, 121000, "배포 로그는 어때요")], 123000)
    assert o.process() == 1, "새 줄 2개: 바로 호출"

    # 4) 맞장구는 앞 줄의 안건에 붙인다. 간투사, 낮은 확신도 줄은 뺀다. 맞장구만 늘어나면 호출하지 않는다
    seen = []
    c, o = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 2500, "네 좋아요", kind="backchannel"), ln(3, 4000, "배포 일정 확정해요"),
             ln(4, 5500, "음", kind="filler"), ln(5, 6000, "배포 흐릿한 말", low=True), ln(6, 8000, "배포 금요일은 어때요"),
             ln(7, 9000, "배포 위험해요"), ln(8, 30000, "네네", kind="backchannel")], 31000)
    o.process()
    u = seen[-1]
    assert "[맞장구] 네 좋아요" in u and "흐릿한" not in u and "네네" not in u, u
    n = o.calls
    feed(c, [ln(9, 11000, "네 그렇죠", kind="backchannel")], 12000)
    assert o.process() == 0 and o.calls == n, "맞장구만 늘어남"

    # 5) 근거 번호가 범위 밖이면 오류를 알려 한 번 더 부른다. 고치면 반영
    inputs = []
    bad = ans(("배포 의견", "OPINION", [[9, 9]]))
    good = ans(("배포를 하자는 의견", "OPINION", [[1, 2]]))
    answers = [bad, good]
    c, o = fresh(llm=lambda u: inputs.append(u) or answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and "[검증 오류]" in inputs[1] and "[이전 출력]" in inputs[1]
    assert [x["evidenceSeqs"] for x in items(o)] == [[1, 2]] and o.rejected == 0
    # 6) 다시 불러도 틀리면 그 항목은 내지 않는다 (연속 실패로 세지 않음). 통과한 항목은 남는다
    answers = [bad, bad]
    c, o = fresh(llm=lambda u: answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and items(o) == [] and o.rejected == 1 and o.err_streak == 0 and not o.halted
    mixed = ans(("배포를 하자는 의견", "OPINION", [[1, 1]]), ("배포 의견", "OPINION", [[9, 9]]))
    answers = [mixed, mixed]
    c, o = fresh(llm=lambda u: answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and [x["evidenceSeqs"] for x in items(o)] == [[1]] and o.rejected == 1
    # 7) kind 가 틀리거나 전사에 없는 숫자가 든 항목도 같다
    answers = [ans(("배포 의견", "MAYBE", [[1, 1]])), ans(("배포 의견", "MAYBE", [[1, 1]]))]
    inputs = []
    c, o = fresh(llm=lambda u: inputs.append(u) or answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and "kind" in inputs[1] and items(o) == [] and o.rejected == 1
    fab = ans(("예산을 10000원으로 하자는 의견", "OPINION", [[1, 1]]))
    answers = [fab, fab]
    c, o = fresh(llm=lambda u: answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and items(o) == [] and o.rejected == 1
    # 8) 형식 오류(JSON 아님, opinions 가 목록이 아님)도 한 번 더, 둘 다 틀리면 실패
    answers = ["JSON 아님", json.dumps({"opinions": "x"})]
    c, o = fresh(llm=lambda u: answers.pop(0))
    feed(c, BASE, 13000)
    assert o.process() == 2 and o.errors == 1 and items(o) == [] and not o.halted

    # 9) 호출이 연속 3번 실패하면 이 기능만 멈춘다. 성공하면 연속 횟수가 0으로 돌아간다
    def boom(u):
        raise RuntimeError("키 오류 기밀단어")
    c, o = fresh(llm=boom, min_lines=1, min_new_lines=1)
    feed(c, [ln(1, 0, "배포 얘기")], 1000)
    for k in range(5):
        feed(c, [ln(10 + k, 2000 + k * 1000, "배포 계속")], 3000 + k * 1000)
        o.process()
    assert o.halted == "연속 실패" and o.calls == 3 and o.errors == 3, (o.halted, o.calls)
    plan = ["x", "x", "ok", "x", "x"]

    def flaky(u):
        r = plan.pop(0)
        if r == "x":
            raise RuntimeError("실패")
        return echo_llm(u)
    c, o = fresh(llm=flaky, min_lines=1, min_new_lines=1)
    for k in range(5):
        feed(c, [ln(1 + k, k * 1000, "배포 계속")], k * 1000 + 500)
        o.process()
    assert o.calls == 5 and o.errors == 4 and not o.halted, (o.calls, o.errors, o.halted)
    # 10) 호출 상한
    c, o = fresh(llm=echo_llm, max_calls=1, min_lines=1)
    feed(c, [ln(1, 0, "배포 얘기"), ln(2, 1000, "배포 계속"), ln(3, 2000, "비용 얘기"), ln(4, 3000, "비용 계속")], 5000)
    assert o.process() == 1 and o.calls == 1 and o.halted == "호출 상한"

    # 11) 사람이 고친 항목: 고정, 모델에는 "이미 있는 항목"으로, 낡은 rev·고치지 않은 항목의 confirm 은 무시
    seen = []
    c, o = fresh(llm=lambda u: seen.append(u) or echo_llm(u))
    feed(c, BASE, 13000)
    o.process()
    r = o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "text": "사람이 쓴 의견", "kind": "CONCERN",
                        "evidence_seqs": [1, 2], "rev": 5}])
    assert r == {"applied": 1, "ignored": []}, r
    r = o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "text": "다른 글", "kind": "OPINION",
                        "evidence_seqs": [1], "rev": 5},
                       {"agenda_id": "a1", "item_id": "nope", "action": "confirm", "rev": 1}])
    assert r == {"applied": 0, "ignored": [{"itemId": "old-1", "reason": "stale"}, {"itemId": "nope", "reason": "not_edited"}]}, r
    assert o.process() == 1, "고친 안건은 기다리지 않고 다시 처리"
    u = seen[-1]
    assert "[사람이 확정한 항목" in u and "- (근거 줄 1~2) [CONCERN] 사람이 쓴 의견" in u, u
    b = items(o)
    assert b[0] == {"id": "old-1", "text": "사람이 쓴 의견", "kind": "CONCERN", "evidenceSeqs": [1, 2], "edited": True,
                    "needsCheck": False}, b
    assert len(b) == 5 and all(x["id"].startswith("p-o") and not x["edited"] for x in b[1:]), "고친 항목이 맨 앞, AI 항목이 뒤따름"
    # 12) needsCheck (가) 근거 줄의 글이 바뀌면 켜진다. confirm 으로 꺼지고 다시 켜지지 않는다
    feed(c, [], 14000, edits=[{"id": 1, "rev": 1, "text": "배포는 금요일로 하죠", "text_marked": "배포는 금요일로 하죠"}])
    assert [x["needsCheck"] for x in items(o) if x["id"] == "old-1"] == [True]
    assert o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "confirm", "rev": 6}])["applied"] == 1
    assert o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "confirm", "rev": 6}])["ignored"][0]["reason"] == "stale"
    o.process()
    assert [x["needsCheck"] for x in items(o) if x["id"] == "old-1"] == [False], "confirm 뒤 다시 켜짐"
    # 13) (나) 근거 줄이 다른 안건으로 옮겨 가면 켜진다. 항목은 그대로 둔다
    c.apply_corrections([{"seq": 2, "agenda_id": "a2", "rev": 1}])
    o.process()
    m = [x for x in items(o) if x["id"] == "old-1"][0]
    assert m["needsCheck"] is True and m["text"] == "사람이 쓴 의견" and m["edited"] is True, m
    o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "confirm", "rev": 7}])
    o.process()
    assert [x["needsCheck"] for x in items(o) if x["id"] == "old-1"] == [False]
    # 14) (다) 근거 줄이 사라져도 켜진다. 확인하면 꺼지고 사라진 줄 때문에 다시 켜지지 않는다
    feed(c, [], 15000, edits=[{"id": 1, "rev": 2, "deleted": True}])
    o.process()
    assert [x["needsCheck"] for x in items(o) if x["id"] == "old-1"] == [True]
    o.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "confirm", "rev": 8}])
    o.process()
    assert [x["needsCheck"] for x in items(o) if x["id"] == "old-1"] == [False]
    assert [x["id"] for x in items(o)] == ["old-1"], "줄이 4개 아래로 줄면 AI 항목은 비우고 고친 항목만 남김"

    # 15) 줄이 다른 안건으로 옮겨 가면 기다리지 않고 남은 안건을 다시 처리한다. 옮겨 간 줄은 근거에서 빠진다
    c, o = fresh(llm=echo_llm)
    feed(c, BASE + [ln(5, 16000, "배포 금요일로 하죠")], 17000)
    o.process()
    n = o.calls
    c.apply_corrections([{"seq": 4, "agenda_id": "a2", "rev": 1}])
    assert o.process() == 1 and o.calls == n + 1, "줄이 빠진 안건은 바로 다시 처리 (옮겨 온 안건은 줄이 적어 호출 안 함)"
    assert all(4 not in x["evidenceSeqs"] for x in items(o)) and items(o, "a2") == []

    # 16) 안건이 목록에서 사라지면 고친 항목까지 버린다. 목록에 없던 안건의 고침은 보관한다
    c, o = fresh(llm=echo_llm)
    o.apply_edits([{"agenda_id": "a1", "item_id": "k-1", "action": "edit", "text": "사라질 항목", "kind": "OPINION",
                    "evidence_seqs": [1], "rev": 1},
                   {"agenda_id": "zz", "item_id": "z-1", "action": "edit", "text": "늦게 온 안건의 항목", "kind": "OPINION",
                    "evidence_seqs": [3], "rev": 1}])
    o.process()
    assert "a1" in o.ag and "zz" in o.ag, "목록에 없던 안건의 고침을 버림"
    c.set_agendas(2, [A2, ag("zz", "새 안건", "아무거나", 2)])
    o.process()
    assert [x["id"] for x in items(o, "zz")] == ["z-1"] and "a1" not in o.ag, o.ag.keys()
    c.set_agendas(3, [A2])
    o.process()
    assert "zz" not in o.ag and set(o.board_items()) == {"a2"}

    # 17) 안건당 항목 수 상한: 고친 항목이 먼저 자리를 차지한다. 다 차지하면 모델을 부르지 않는다
    c, o = fresh(llm=echo_llm, max_items=2)
    feed(c, BASE, 13000)
    o.apply_edits([{"agenda_id": "a1", "item_id": "k-1", "action": "edit", "text": "고친 항목", "kind": "OPINION",
                    "evidence_seqs": [1], "rev": 1}])
    assert o.process() == 1
    assert [x["id"] for x in items(o)][0] == "k-1" and len(items(o)) == 2
    o.apply_edits([{"agenda_id": "a1", "item_id": "k-2", "action": "edit", "text": "또 고친 항목", "kind": "OPINION",
                    "evidence_seqs": [2], "rev": 1}])
    assert o.process() == 0 and [x["id"] for x in items(o)] == ["k-1", "k-2"]

    # 18) 마지막(final)에는 기다리던 새 줄도 처리한다. 줄이 4개 미만이면 final 이어도 부르지 않는다
    c, o = fresh(llm=echo_llm)
    feed(c, BASE, 13000)
    o.process()
    feed(c, [ln(5, 16000, "배포 다시 하죠")], 17000)
    assert o.process() == 0 and o.process(final=True) == 1
    c, o = fresh(llm=echo_llm)
    feed(c, BASE[:3], 9000)
    assert o.process(final=True) == 0

    # 19) 숫자 안건 번호도 key 문자열로 돌려준다 (보드에서 타입을 되돌리는 것은 안건 요약기가 한다)
    c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
    c.set_agendas(1, [{"id": 101, "key": "101", "position": 1, "title": "배포 일정", "decision_point": "배포 날짜", "added": False}])
    o = AgendaOpinions(c, OpinionCfg(), id_prefix="p")
    feed(c, BASE, 13000)
    o.process()
    assert [x["kind"] for x in o.board_items()["101"]] == ["OPINION", "EVIDENCE", "CONCERN", "QUESTION"]

    # 20) 프롬프트, 설정, info
    for w in ("OPINION", "EVIDENCE", "CONCERN", "QUESTION", "지어내지 마", "사람이 확정한 항목", "JSON만"):
        assert w in SYSTEM_PROMPT, w
    d = OpinionCfg()
    assert (d.min_lines, d.max_items, d.refresh_sec, d.min_new_lines, d.max_calls, d.provider) == (4, 8, 60, 2, 300, "echo")
    try:
        OpinionCfg(nope=1)
        assert False
    except TypeError:
        pass
    info = o.info()
    assert set(info) == {"agendas", "items", "edited", "needsCheck", "calls", "errors", "rejected", "halted"}, info

    # 21) 로그에는 전사, 안건 제목, 항목 문장이 남지 않는다
    logging.getLogger("ai-service").removeHandler(h)
    text = "\n".join(records)
    for secret in ("기밀단어", "배포 일정", "사람이 쓴 의견", "늦게 온 안건의 항목", "금요일은 어때요"):
        assert secret not in text, f"로그에 내용이 남음: {secret}"
    assert records, "로그가 하나도 없음 (검사가 의미 없음)"
    print("agenda_opinions selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
