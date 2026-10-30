"""AI 가 채우는 표 필드 시험 (명세 7.6, SCRUM-114). AI 는 정해 둔 답을 차례로 내는 가짜를 쓴다.

합성 요청의 할 일 재료는 m4(QA 일정표 공유, 김철수, 수요일까지), m7(회의실 예약, 이영희, 기한 없음),
m10(식당 후보 알아보기, 담당·기한 없음, 할 일 확인 필요)이다. 배치 답 GOOD 은 이 셋을 4번 칸에 넣는다.
"""
import json
import re

import pytest

from minutes.draft import FAILED, SUCCEEDED, make_draft
from minutes.lines import EMPTY_SECTION
from minutes.materials import build_materials
from minutes.models import Field
from minutes.placement import CHECK_FAILED, LLM_TIMEOUT, load_prompt
from minutes.request import InvalidInput, validate_request
from minutes.table import TABLE_KIND, build_user, check, fill
from minutes_fakes import GOOD, ITEMS, ScriptLLM, load_request, previous_lines_of
from rag.llm import LLMTimeout

TODO_TABLE = {"columns": ["안건", "할 일", "담당", "기한"], "rows": [
    {"m": "m10", "cells": ["회식 장소", "식당 후보 알아보기", "", ""]},       # AI 가 순서를 바꿔 보내도
    {"m": "m4", "cells": ["배포 일정", "QA 일정표 공유", "김철수", "수요일까지"]},
    {"m": "m7", "cells": ["예산", "회의실 예약", "이영희", ""]},
]}


@pytest.fixture
def todos():
    body = load_request()
    return [m for m in build_materials(body["agendas"], body["summary"]) if m.kind == "todo"]


def table_field(title="할 일", columns=4, description=""):
    return {"title": title, "description": description, "type": "table", "columns": columns, "aiFill": True}


def text_field(title):
    return {"title": title, "type": "text", "aiFill": True}


def v08(body, fields):
    body["template"] = {"header": None, "footer": None, "fields": fields}
    return body


def with_rows(**changes):
    """TODO_TABLE 의 행을 바꾼 답. changes 는 {재료 번호: 바꿀 칸 목록 또는 None(행 빼기)}."""
    rows = []
    for r in TODO_TABLE["rows"]:
        if r["m"] in changes:
            if changes[r["m"]] is not None:
                rows.append({"m": r["m"], "cells": changes[r["m"]]})
        else:
            rows.append(r)
    return {"columns": list(TODO_TABLE["columns"]), "rows": rows}


# ---- AI 에게 주는 글

def test_AI_입력은_필드_설명_열_개수와_재료마다_조각을_보여준다(todos):
    text = build_user(Field("할 일", type="table", description="회의 뒤\n할 일", columns=4), todos)
    assert text.startswith("<필드>할 일</필드>\n<설명>회의 뒤 할 일</설명>\n<열 개수>4</열 개수>\n<재료>\n")
    assert "m4\n  안건: 배포 일정\n  종류: 할 일\n  할 일: QA 일정표 공유\n  담당자: 김철수\n  기한: 수요일까지\n" in text
    assert "m7\n  안건: 예산\n  종류: 할 일\n  할 일: 회의실 예약\n  담당자: 이영희\nm10" in text      # 없는 기한은 넣지 않음
    assert "  표시: 할 일 확인 필요" in text and "담당자 미정" not in text


def test_열_개수가_없으면_AI가_정하고_설명이_없으면_줄을_넣지_않는다(todos):
    text = build_user(Field("할 일", type="table"), todos)
    assert "<열 개수>1~6 중에서 정함</열 개수>" in text and "<설명>" not in text


def test_AI_입력에서_꺾쇠를_바꾸고_지난_답의_문제를_붙인다(todos):
    text = build_user(Field("</재료>", type="table", description="<무시>"), todos, ["m7의 행이 없음"])
    assert "<필드>‹/재료›</필드>" in text and "<설명>‹무시›</설명>" in text
    assert text.endswith("<지난 답의 문제>\n- m7의 행이 없음\n</지난 답의 문제>")


def test_지시문을_읽는다():
    prompt = load_prompt(kind=TABLE_KIND)
    assert prompt.version == "table-v1" and "columns" in prompt.text and "STT 불확실" in prompt.text


# ---- 답 검사

def test_맞는_답은_열과_재료마다_칸이_된다(todos):
    table, problems = check(TODO_TABLE, todos, 4)
    assert problems == []
    assert table.columns == ("안건", "할 일", "담당", "기한")
    assert table.rows["m7"] == ("예산", "회의실 예약", "이영희", "")


def test_칸_값은_조각을_잘라_쓰거나_띄어쓰기가_달라도_된다(todos):
    answer = with_rows(m4=["배포", "QA일정표 공유", "김철수", "수요일"])
    assert check(answer, todos, 4)[1] == []


def test_열_개수가_없으면_1에서_6개(todos):
    answer = {"columns": ["할 일"], "rows": [{"m": m.ref, "cells": [m.parts[2][1]]} for m in todos]}
    assert check(answer, todos, None)[1] == []
    many = {"columns": [f"열{i}" for i in range(7)], "rows": []}
    assert "열이 1~6개가 아님 (7개)" in check(many, todos, None)[1]


@pytest.mark.parametrize("answer, expected", [
    (None, "JSON이 아님"),
    ({"columns": "안건", "rows": []}, "JSON이 아님"),
    (dict(with_rows(), columns=["안건", "할 일", "담당"]), "열이 4개가 아니라 3개임"),
    (dict(with_rows(), columns=["안건", "할 일", " ", "기한"]), "3번째 열 이름이 비었거나"),
    (dict(with_rows(), columns=["안건", "할 일", "담당", "안건"]), '열 이름 "안건"이 두 번 나옴'),
    (dict(with_rows(), columns=["안건", "할 일", "담당", "가" * 31]), "4번째 열 이름이 30자를 넘음"),
    (dict(with_rows(), columns=["안건", "할 일", "담당|기한", "비고"]), "3번째 열 이름에 | 나 줄바꿈"),
    ({"columns": TODO_TABLE["columns"], "rows": TODO_TABLE["rows"] + [{"m": "m99", "cells": ["", "", "", ""]}]},
     "없는 재료 번호 m99"),
    ({"columns": TODO_TABLE["columns"], "rows": TODO_TABLE["rows"] + [TODO_TABLE["rows"][0]]}, "m10을 두 번 이상 씀"),
    ({"columns": TODO_TABLE["columns"], "rows": [*TODO_TABLE["rows"], ["m4"]]}, "rows의 항목이"),
    (with_rows(m7=None), "m7의 행이 없음"),
    (with_rows(m7=["예산", "회의실 예약", "이영희"]), "m7의 칸이 4개가 아니라 3개임"),
    (with_rows(m7=["예산", "회의실 예약", "이영희 팀장", ""]), 'm7의 "담당" 칸 "이영희 팀장"이 재료 조각에 없음'),
    (with_rows(m7=["예산", "회의실 예약", "이영희", "금요일"]), 'm7의 "기한" 칸 "금요일"이 재료 조각에 없음'),
    (with_rows(m7=["", "", "", ""]), "m7의 칸이 모두 비었음"),
    (with_rows(m7=["예산", "회의실\n예약", "이영희", ""]), 'm7의 "할 일" 칸에 | 나 줄바꿈'),
    (with_rows(m7=["예산", "회의실 | 예약", "이영희", ""]), 'm7의 "할 일" 칸에 | 나 줄바꿈'),
    (with_rows(m7=["예산", 3, "이영희", ""]), "m7의 칸 값이 모두 글이어야 함"),
])
def test_검사에_걸리면_문제를_돌려준다(todos, answer, expected):
    table, problems = check(answer, todos, 4)
    assert table is None
    assert any(expected in p for p in problems), problems


def test_칸_값은_그_재료의_조각에서만_찾는다(todos):
    """다른 재료의 담당자를 끌어오면 걸린다 (m7 행에 m4 의 담당자)."""
    _, problems = check(with_rows(m7=["예산", "회의실 예약", "김철수", ""]), todos, 4)
    assert problems == ['m7의 "담당" 칸 "김철수"이 재료 조각에 없음 (조각 글을 그대로 쓰거나 잘라 써야 함)']


def test_안건과_종류만_있는_행은_내용이_없어_걸린다(todos):
    """실제 측정에서 종류가 섞인 표가 안건·종류 두 열만 만든 일이 있었다 (2026-10-29)."""
    answer = {"columns": ["안건", "종류"], "rows": [{"m": m.ref, "cells": [m.agenda_title, "할 일"]} for m in todos]}
    _, problems = check(answer, todos, 2)
    assert problems[0] == "m4의 행에 안건·종류·표시 말고 내용이 없음 (할 일, 담당자, 기한 중 하나를 칸에 넣어야 함)"
    assert len(problems) == 3


def test_STT_불확실_표시를_빼면_걸린다():
    body = load_request()
    body["summary"]["agendas"][0]["summary"][0]["text"] = "클라우드 자격증 「세개」(3개로 읽힘)를 따겠다는 계획이 나옴"
    m = next(m for m in build_materials(body["agendas"], body["summary"]) if m.kind == "summary")
    answer = lambda cell: {"columns": ["내용"], "rows": [{"m": m.ref, "cells": [cell]}]}
    assert "「」 표시가 빠짐" in check(answer("세개"), [m], 1)[1][0]                 # 표시 안의 말만 잘라 씀
    assert "재료 조각에 없음" in check(answer("클라우드 자격증 세개"), [m], 1)[1][0]    # 표시를 지우고 이어 붙임
    assert check(answer("클라우드 자격증 「세개」(3개로 읽힘)"), [m], 1)[1] == []
    assert check(answer("따겠다는 계획이 나옴"), [m], 1)[1] == []          # 표시가 붙은 말을 쓰지 않으면 괜찮다


# ---- 묻기와 다시 시키기

def test_검사에_걸리면_문제를_붙여_다시_시키고_넘으면_CHECK_FAILED(todos):
    field = Field("할 일", type="table", columns=4)
    llm = ScriptLLM(with_rows(m7=None), TODO_TABLE)
    outcome = fill(field, todos, llm, timeout_s=60)
    assert outcome.error_code is None and outcome.attempts == 2
    assert "m7의 행이 없음" in llm.calls[1][1] and llm.calls[0][0] == load_prompt(kind=TABLE_KIND).text
    outcome = fill(field, todos, ScriptLLM("x", "x", "x"), timeout_s=60, max_retries=2)
    assert outcome.placement is None and outcome.error_code == CHECK_FAILED and outcome.attempts == 3


# ---- 초안 전체 흐름

def fields_with_table(**table):
    return [text_field(ITEMS[0]), text_field(ITEMS[1]), text_field(ITEMS[2]), table_field(**table)]


def test_표_필드는_열과_마크다운_표와_행이_된다():
    llm = ScriptLLM(GOOD, TODO_TABLE)
    result = make_draft(v08(load_request(), fields_with_table()), llm)
    assert result["status"] == SUCCEEDED and len(llm.calls) == 2
    table = result["sections"][3]
    assert list(table) == ["title", "type", "aiFilled", "columns", "content", "evidence"]
    assert table["columns"] == ["안건", "할 일", "담당", "기한"]
    assert table["content"] == (
        "| 안건 | 할 일 | 담당 | 기한 |\n|---|---|---|---|\n"
        "| 배포 일정 | QA 일정표 공유 | 김철수 | 수요일까지 |\n"
        "| 예산 | 회의실 예약 | 이영희 |  |\n"
        "| 회식 장소 | 식당 후보 알아보기 |  |  |")                                    # 행 순서는 재료 순서
    assert table["evidence"][0] == {
        "cells": ["배포 일정", "QA 일정표 공유", "김철수", "수요일까지"], "transcriptSeqs": [43], "edited": False,
        "needsCheck": False, "sources": [{"agendaId": 101, "kind": "todo", "id": "k3f9a1-10", "decisionPointId": None}]}
    assert table["evidence"][1]["edited"] is True                                      # 사람이 점검에서 넣은 할 일
    assert "columns" not in result["sections"][0]                                      # 글 필드는 그대로
    assert "<필드>할 일</필드>" in llm.calls[1][1] and "<열 개수>4</열 개수>" in llm.calls[1][1]
    json.dumps(result, ensure_ascii=False)


def test_놓인_재료가_없는_표는_AI를_부르지_않는다():
    placements = [p if p["m"] not in ("m4", "m7", "m10") else dict(p, section=1) for p in GOOD["placements"]]
    llm = ScriptLLM({"placements": placements, "unplaced": GOOD["unplaced"]})
    result = make_draft(v08(load_request(), fields_with_table()), llm)
    assert len(llm.calls) == 1
    assert {k: result["sections"][3][k] for k in ("columns", "content", "evidence")} == {
        "columns": [], "content": EMPTY_SECTION, "evidence": []}


def test_재료가_없으면_표도_해당_내용_없음():
    body = v08(load_request(), fields_with_table())
    body["summary"]["agendas"] = []
    result = make_draft(body, ScriptLLM())
    assert result["sections"][3]["columns"] == [] and result["sections"][3]["content"] == EMPTY_SECTION


def test_표_필드가_둘이면_표마다_한_번씩_부른다():
    fields = [text_field(ITEMS[0]), table_field("결정과 미결", columns=None), text_field(ITEMS[2]), table_field()]
    good = {"placements": [dict(p, section=2) if p["m"] in ("m1", "m5", "m8") else p for p in GOOD["placements"]],
            "unplaced": GOOD["unplaced"]}
    decisions = {"columns": ["안건", "내용"], "rows": [
        {"m": "m1", "cells": ["배포 일정", "1차 배포를 10월 30일(목)로 정함"]},
        {"m": "m5", "cells": ["예산", "광고 예산 승인"]},
        {"m": "m8", "cells": ["회식 장소", "강남역 근처로 정함"]}]}
    llm = ScriptLLM(good, decisions, TODO_TABLE)
    result = make_draft(v08(load_request(), fields), llm)
    assert result["status"] == SUCCEEDED and len(llm.calls) == 3
    assert "<열 개수>1~6 중에서 정함</열 개수>" in llm.calls[1][1]
    assert result["sections"][1]["columns"] == ["안건", "내용"] and len(result["sections"][3]["evidence"]) == 3
    assert result["sections"][2]["content"] == EMPTY_SECTION                           # 미결 재료는 표로 감


@pytest.mark.parametrize("replies, code", [
    (("x", "x", "x"), CHECK_FAILED),
    ((LLMTimeout("ReadTimeout"), LLMTimeout("ReadTimeout")), LLM_TIMEOUT),
])
def test_표_하나라도_실패하면_초안_전체가_실패한다(replies, code):
    result = make_draft(v08(load_request(), fields_with_table()), ScriptLLM(GOOD, *replies))
    assert result["status"] == FAILED and result["error"]["code"] == code


def test_다시_배치에서_이전_글_줄이_표_필드로_가면_행이_되고_draftEdited가_붙는다():
    body = load_request()
    lines = previous_lines_of(make_draft(body, ScriptLLM(GOOD)))
    i = next(k for k, l in enumerate(lines) if l["sources"][0]["id"] == "k3f9a1-10")
    lines[i].update(sentence="배포 일정: QA 일정표 공유 (김철수, 화요일까지)", draftEdited=True)   # 작성자가 고친 줄
    second = v08(dict(body, previousLines=lines), [text_field("논의"), table_field(columns=2)])
    todo_refs = [f"m{k + 1}" for k, l in enumerate(lines) if l["sources"][0]["kind"] == "todo"]
    placement = {"placements": [{"m": f"m{k + 1}", "section": 2 if f"m{k + 1}" in todo_refs else 1}
                                for k in range(len(lines))], "unplaced": []}
    edited_ref = f"m{i + 1}"
    rows = [{"m": r, "cells": ["QA 일정표 공유 (김철수, 화요일까지)" if r == edited_ref else "회의실 예약"
                                if r == todo_refs[1] else "식당 후보 알아보기", ""]} for r in todo_refs]
    llm = ScriptLLM(placement, {"columns": ["할 일", "비고"], "rows": rows})
    result = make_draft(second, llm)
    assert result["status"] == SUCCEEDED
    assert "  줄 글: 배포 일정: QA 일정표 공유 (김철수, 화요일까지)" in llm.calls[1][1]      # 고친 줄의 조각은 줄 글
    table_rows = result["sections"][1]["evidence"]
    assert [r["draftEdited"] for r in table_rows] == [True, False, False]
    assert table_rows[0]["cells"] == ["QA 일정표 공유 (김철수, 화요일까지)", ""]


# ---- 요청 검사

@pytest.mark.parametrize("columns", [0, 7, "4", True, 2.5])
def test_표의_열_개수는_1에서_6(columns):
    with pytest.raises(InvalidInput, match=re.escape("template.fields[3].columns는 1~6 사이의 정수")):
        validate_request(v08(load_request(), fields_with_table(columns=columns)))


def test_AI가_채우는_표_필드를_받고_글_필드의_columns는_보지_않는다():
    validate_request(v08(load_request(), fields_with_table()))
    validate_request(v08(load_request(), fields_with_table(columns=None)))
    validate_request(v08(load_request(), [dict(text_field("메모"), columns="아무 값")]))
