"""전체 흐름 시험: 요청 dict → 결과 콜백 본문 dict. AI 는 정해 둔 답을 내는 가짜를 쓴다."""
import json
import logging

import pytest

from minutes.draft import FAILED, INPUT_TOO_LONG, MAX_MATERIALS, SUCCEEDED, make_draft
from minutes.lines import EMPTY_SECTION
from minutes.placement import load_prompt
from minutes_fakes import GOOD, ScriptLLM, load_request, previous_lines_of
from rag.llm import LLMError, LLMTimeout


@pytest.fixture
def body():
    return load_request()


def test_정상이면_양식_칸마다_줄과_글이_나온다(body):
    llm = ScriptLLM(GOOD)
    result = make_draft(body, llm)
    assert result["requestId"] == body["requestId"] and result["status"] == SUCCEEDED
    assert [s["title"] for s in result["sections"]] == body["template"]["items"]
    by_title = {s["title"]: s for s in result["sections"]}
    assert by_title["결정 사항"]["content"] == "- 배포 일정: 1차 배포를 10월 30일(목)로 정했다."
    assert by_title["미결 안건"]["content"] == (
        "- 예산: 광고 예산 승인\n"
        "- 회식 장소: (결정 확인 필요: 강남역 근처로 하자는 말은 있으나 합의 표현이 없음)")
    assert result["unplaced"] == [{"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20"}]
    assert result["unplacedLines"] == []
    assert len(llm.calls) == 1
    json.dumps(result, ensure_ascii=False)


def test_결과의_모든_줄은_content와_같고_근거가_있거나_사람이_넣은_것(body):
    result = make_draft(body, ScriptLLM(GOOD))
    for s in result["sections"]:
        assert s["content"] == ("\n".join(f"- {line['sentence']}" for line in s["evidence"]) or EMPTY_SECTION)
        for line in s["evidence"]:
            assert line["transcriptSeqs"] or line["edited"]


def test_재료가_없으면_AI를_부르지_않고_모든_칸이_빈다(body):
    body["summary"]["agendas"] = []
    llm = ScriptLLM()
    result = make_draft(body, llm)
    assert result["status"] == SUCCEEDED and llm.calls == []
    assert all(s["content"] == EMPTY_SECTION and s["evidence"] == [] for s in result["sections"])


def test_재료가_상한을_넘으면_AI를_부르지_않고_실패(body):
    agenda = body["summary"]["agendas"][0]
    agenda["summary"] = [{"id": f"s{i}", "text": f"요약 {i}", "evidenceSeqs": [i], "edited": False, "needsCheck": False}
                         for i in range(MAX_MATERIALS)]
    llm = ScriptLLM()
    result = make_draft(body, llm)
    assert result["status"] == FAILED and result["error"]["code"] == INPUT_TOO_LONG
    assert llm.calls == []


@pytest.mark.parametrize("replies, code", [
    ((LLMTimeout("ReadTimeout"), LLMTimeout("ReadTimeout")), "LLM_TIMEOUT"),
    ((LLMError("APIError"), LLMError("APIError")), "LLM_FAILED"),
    (("x", "x", "x"), "CHECK_FAILED"),
])
def test_AI가_실패하면_실패_본문(body, replies, code):
    result = make_draft(body, ScriptLLM(*replies))
    assert result == {"requestId": body["requestId"], "status": FAILED,
                      "error": {"code": code, "message": result["error"]["message"]}}
    assert result["error"]["message"]


def test_시간_제한과_다시_시키기_횟수를_넘긴다(body):
    llm = ScriptLLM("x", GOOD)
    result = make_draft(body, llm, timeout_s=12.5, max_retries=1)
    assert result["status"] == SUCCEEDED
    assert [c[2] for c in llm.calls] == [12.5, 12.5]


def test_로그에_회의_내용이_남지_않는다(body, caplog):
    caplog.set_level(logging.INFO, logger="minutes.draft")
    make_draft(body, ScriptLLM(GOOD))
    make_draft(body, ScriptLLM("x", "x", "x"))
    text = caplog.text
    assert "초안 완료" in text and "CHECK_FAILED" in text and load_prompt().version in text
    for word in ["배포", "QA", "예산", "회식", "김철수", "안건별 논의", "결정 사항"]:
        assert word not in text


# 다시 배치 (명세 8장). 처음 초안의 줄 순서: 논의 3줄(m2 m3 m6) → 결정 1줄(m1) → 미결 2줄(m5 m8) → 할 일 3줄(m4 m7 m10)
NEW_ITEMS = ["논의", "결정과 할 일"]


def relayout_body(body, lines):
    return dict(body, requestId="relayout-1", template={"items": NEW_ITEMS}, previousLines=lines)


def answer(extra=(), unplaced=("m6",)):
    """이전 줄 9개(+더한 줄)의 새 양식 배치. 4번째 줄(m4)이 다시 다듬는 결정, m6(결정 확인 필요)은 넣지 못함"""
    placements = [{"m": f"m{i}", "section": 1} for i in (1, 2, 3, 5)]
    placements += [{"m": "m4", "section": 2, "text": "1차 배포를 10월 30일(목)로 정했다."}]
    placements += [{"m": f"m{i}", "section": 2} for i in (7, 8, 9)] + list(extra)
    return {"placements": placements, "unplaced": list(unplaced)}


def test_다시_배치는_이전_줄을_새_칸에_옮기고_draftEdited를_붙인다(body):
    first = make_draft(body, ScriptLLM(GOOD))
    lines = previous_lines_of(first)
    result = make_draft(relayout_body(body, lines), ScriptLLM(answer()))
    assert result["status"] == SUCCEEDED and [s["title"] for s in result["sections"]] == NEW_ITEMS
    talk, decided = result["sections"]
    assert [l["sentence"] for l in talk["evidence"]] == [lines[i]["sentence"] for i in (0, 1, 2, 4)]
    assert decided["evidence"][0]["sentence"] == "배포 일정: 1차 배포를 10월 30일(목)로 정했다."
    assert all(l["draftEdited"] is False for s in result["sections"] for l in s["evidence"])
    assert result["unplacedLines"] == [lines[5]]                                  # 받은 줄 그대로
    assert result["unplaced"] == [{"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20"}]   # 처음에도 못 넣은 의견


def test_고친_줄과_새로_쓴_줄은_글이_그대로_남는다(body):
    lines = previous_lines_of(make_draft(body, ScriptLLM(GOOD)))
    lines[3].update(sentence="배포 일정: 1차 배포를 10월 30일(목) 오후로 정했다.", draftEdited=True)
    lines.append({"title": "할 일", "sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [],
                  "edited": False, "needsCheck": False, "sources": [], "draftEdited": True})
    llm = ScriptLLM(answer(extra=[{"m": "m10", "section": 2}]))
    result = make_draft(relayout_body(body, lines), llm)
    decided = result["sections"][1]["evidence"]
    assert decided[0]["sentence"] == "배포 일정: 1차 배포를 10월 30일(목) 오후로 정했다."   # 제목이 두 번 붙지 않음
    assert decided[0]["draftEdited"] is True and decided[0]["sources"] == lines[3]["sources"]
    assert decided[-1] == {"sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [], "edited": False,
                           "needsCheck": False, "sources": [], "draftEdited": True}
    assert "m4 [배포 일정 / 결정] (고정)" in llm.calls[0][1] and "m10 [직접 작성] (고정)" in llm.calls[0][1]


def test_다시_배치의_상한은_이전_줄_수로_센다(body):
    lines = previous_lines_of(make_draft(body, ScriptLLM(GOOD)))
    many = (lines * 40)[:MAX_MATERIALS + 1]
    llm = ScriptLLM()
    result = make_draft(relayout_body(body, many), llm)
    assert result["status"] == FAILED and result["error"]["code"] == INPUT_TOO_LONG and llm.calls == []


def test_previousLines가_빈_목록이면_처음_만들기(body):
    result = make_draft(dict(body, previousLines=[]), ScriptLLM(GOOD))
    assert "draftEdited" not in result["sections"][1]["evidence"][0] and result["unplacedLines"] == []
