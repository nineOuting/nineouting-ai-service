"""양식 필드 시험 (명세 v0.7 2.1·5.4·5.8, SCRUM-113): 필드마다 AI 채움 설정, 사용자가 채우는 필드, 머리글·바닥글.
v0.6 요청(template.items)은 결과도 v0.6 모양 그대로여야 한다 (백엔드가 새 칸을 모를 수 있음)."""
import re

import pytest

from minutes.draft import SUCCEEDED, make_draft
from minutes.lines import EMPTY_SECTION
from minutes.request import InvalidInput, validate_request
from minutes_fakes import GOOD, ITEMS, ScriptLLM, load_request, previous_lines_of

MEMO = "다음 회의는 장소를 2층 회의실로 옮김"


def v07(body, fields, header="고객성공팀 주간 회의", footer="사내 공유용"):
    body["template"] = {"header": header, "footer": footer, "fields": fields}
    return body


def ai(title):
    return {"title": title, "type": "text", "aiFill": True}


def user(title, content=MEMO, type_="text"):
    return {"title": title, "type": type_, "aiFill": False, "content": content}


def test_v06_요청은_결과도_v06_모양_그대로():
    result = make_draft(load_request(), ScriptLLM(GOOD))
    assert set(result) == {"requestId", "status", "sections", "unplaced", "unplacedLines"}
    assert all(set(s) == {"title", "content", "evidence"} for s in result["sections"])


def test_v07_요청은_필드마다_종류와_AI_채움이_붙고_머리글_바닥글을_돌려준다():
    body = v07(load_request(), [ai(t) for t in ITEMS])
    result = make_draft(body, ScriptLLM(GOOD))
    assert (result["header"], result["footer"]) == ("고객성공팀 주간 회의", "사내 공유용")
    assert [(s["title"], s["type"], s["aiFilled"]) for s in result["sections"]] == [(t, "text", True) for t in ITEMS]
    assert list(result["sections"][0]) == ["title", "type", "aiFilled", "content", "evidence"]
    v06 = make_draft(load_request(), ScriptLLM(GOOD))
    assert [s["content"] for s in result["sections"]] == [s["content"] for s in v06["sections"]]   # 글은 같음


def test_머리글_바닥글이_없으면_null():
    body = load_request()
    body["template"] = {"fields": [ai(t) for t in ITEMS]}
    result = make_draft(body, ScriptLLM(GOOD))
    assert result["header"] is None and result["footer"] is None


def test_사용자_필드는_AI가_보지_않고_받은_글_그대로_필드_위치도_맞는다():
    """사용자 필드를 AI 필드 사이에 끼운다. AI 는 예전과 같은 칸 4개를 보고 같은 답(GOOD)을 내야 하고,
    결과는 사용자 필드만큼 밀린 위치에 들어가야 한다."""
    fields = [ai(ITEMS[0]), user("참석자 메모"), ai(ITEMS[1]), ai(ITEMS[2]), user("예산표", "| 항목 | 금액 |\n|---|---|\n| 홍보 | 2,000,000원 |", "table"), ai(ITEMS[3])]
    llm = ScriptLLM(GOOD)
    result = make_draft(v07(load_request(), fields), llm)
    user_prompt = llm.calls[0][1]
    assert "참석자 메모" not in user_prompt and "예산표" not in user_prompt
    assert all(f"{i}. {t}" in user_prompt for i, t in enumerate(ITEMS, 1))       # AI 가 보는 번호는 예전 그대로

    by_title = {s["title"]: s for s in result["sections"]}
    assert [s["title"] for s in result["sections"]] == [f["title"] for f in fields]
    assert by_title["참석자 메모"] == {"title": "참석자 메모", "type": "text", "aiFilled": False, "content": MEMO, "evidence": []}
    assert by_title["예산표"]["type"] == "table" and by_title["예산표"]["content"].startswith("| 항목 | 금액 |")
    v06 = {s["title"]: s for s in make_draft(load_request(), ScriptLLM(GOOD))["sections"]}
    for title in ITEMS:
        assert by_title[title]["content"] == v06[title]["content"] and by_title[title]["evidence"] == v06[title]["evidence"]


def test_사용자_필드_글이_없으면_빈_글():
    result = make_draft(v07(load_request(), [ai(t) for t in ITEMS] + [{"title": "서명", "type": "text", "aiFill": False}]),
                        ScriptLLM(GOOD))
    assert result["sections"][-1]["content"] == "" and result["sections"][-1]["evidence"] == []


def test_AI가_채울_필드가_없으면_AI를_부르지_않고_재료는_모두_넣지_못한_것으로():
    llm = ScriptLLM()
    result = make_draft(v07(load_request(), [user("메모"), user("서명", "")]), llm)
    assert result["status"] == SUCCEEDED and llm.calls == []
    assert [s["content"] for s in result["sections"]] == [MEMO, ""]
    assert len(result["unplaced"]) == 10                                       # 합성 요청의 재료 m1~m10


def test_AI가_채울_필드가_없을_때_다시_배치의_이전_줄은_unplacedLines로():
    first = make_draft(load_request(), ScriptLLM(GOOD))
    body = v07(load_request(), [user("메모")])
    body["previousLines"] = previous_lines_of(first)
    llm = ScriptLLM()
    result = make_draft(body, llm)
    assert llm.calls == [] and len(result["unplacedLines"]) == len(body["previousLines"])


def test_재료가_없으면_AI_필드만_해당_내용_없음():
    body = v07(load_request(), [ai("결정 사항"), user("메모")])
    body["summary"]["agendas"] = []
    result = make_draft(body, ScriptLLM())
    assert [s["content"] for s in result["sections"]] == [EMPTY_SECTION, MEMO]


def test_AI가_사용자_필드_번호를_고르면_검사에_걸려_다시_시킨다():
    """AI 는 AI 필드 2개만 본다. 3번(없는 칸)을 고르면 다시 시킨다."""
    body = v07(load_request(), [ai("논의"), user("메모"), ai("기타")])
    refs = [f"m{i}" for i in range(1, 11)]
    bad = {"placements": [{"m": r, "section": 3}
                          for r in refs], "unplaced": []}
    good = {"placements": [{"m": r, "section": 1}
                           for r in refs], "unplaced": []}
    llm = ScriptLLM(bad, good)
    result = make_draft(body, llm)
    assert len(llm.calls) == 2 and "1~2 사이의 칸 번호가 아님" in llm.calls[1][1]
    assert [len(s["evidence"]) for s in result["sections"]] == [10, 0, 0]


@pytest.mark.parametrize("fields, message", [
    ([], "template.fields는 1~20개"),
    ([ai(f"칸{i}") for i in range(21)], "template.fields는 1~20개"),
    (["결정 사항"], "template.fields[0]가 객체가 아님"),
    ([ai("할 일"), ai("할 일 ")], "template.fields[1].title이 앞의 필드와 겹침"),
    ([{"title": "가" * 101, "type": "text", "aiFill": True}], "template.fields[0].title는 100자"),
    ([{"title": "할 일", "type": "list", "aiFill": True}], "template.fields[0].type은 text, table"),
    ([{"title": "할 일", "type": "text", "aiFill": "yes"}], "template.fields[0].aiFill이 true/false"),
    ([{"title": "할 일", "type": "text"}], "template.fields[0].aiFill이 true/false"),
    ([{**ai("할 일"), "description": 3}], "template.fields[0].description는 문자열이나 null"),
    ([{**ai("할 일"), "description": "가" * 2001}], "template.fields[0].description은 2000자"),
    ([user("메모", 3)], "template.fields[0].content는 문자열이나 null"),
    ([user("메모", "가" * 20_001)], "template.fields[0].content는 20000자"),
])
def test_필드_검사(fields, message):
    body = v07(load_request(), fields)
    with pytest.raises(InvalidInput, match=re.escape(message)):
        validate_request(body)


@pytest.mark.parametrize("key, value, message", [
    ("header", 1, "template.header는 문자열이나 null"),
    ("footer", "가" * 20_001, "template.footer는 20000자"),
])
def test_머리글_바닥글_검사(key, value, message):
    body = v07(load_request(), [ai("결정 사항")])
    body["template"][key] = value
    with pytest.raises(InvalidInput, match=re.escape(message)):
        validate_request(body)


def test_필드_설명은_AI가_보는_칸_이름_뒤에_붙고_결과에는_나오지_않는다():
    fields = [{**ai("결정 사항"), "description": "확정된 것만"}, {**user("메모"), "description": "사용자 메모"},
              {**ai("할 일"), "description": ""}]
    llm = ScriptLLM({"placements": [{"m": f"m{i}", "section": 1} for i in range(1, 11)], "unplaced": []})
    result = make_draft(v07(load_request(), fields), llm)
    assert "<칸>\n1. 결정 사항 (설명: 확정된 것만)\n2. 할 일\n</칸>" in llm.calls[0][1]
    assert "사용자 메모" not in llm.calls[0][1]
    assert all("description" not in s for s in result["sections"])


def test_fields가_있으면_items는_보지_않는다():
    body = v07(load_request(), [ai("결정 사항")])
    body["template"]["items"] = []                                              # v0.6 칸이 틀려도 fields 를 쓴다
    validate_request(body)
    result = make_draft(body, ScriptLLM({"placements": [{"m": f"m{i}", "section": 1} for i in range(1, 11)], "unplaced": []}))
    assert [s["title"] for s in result["sections"]] == ["결정 사항"]
