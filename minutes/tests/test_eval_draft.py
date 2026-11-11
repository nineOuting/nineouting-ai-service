"""지시문 평가 도구 시험. AI 는 정해 둔 답을 내는 가짜를 쓴다 (API 를 부르지 않는다).

합성 요청의 맞는 배치 GOOD 은 m9(회식 예산 의견)를 넣지 않는다. 사례 eval_basic.json 의 정답은 안건별 논의라,
GOOD 은 사례 기준으로 한 항목이 틀린 답이다.
"""
import json
from pathlib import Path

import pytest

from minutes import eval_draft
from minutes.draft import make_draft
from minutes.eval_draft import NONE, check_expect, load_case, run_case, score, sent_materials, summarize, variants_of
from minutes.request import validate_request
from minutes_fakes import GOOD, ScriptLLM, load_request, previous_lines_of

CASE = Path(__file__).parent / "fixtures" / "eval_basic.json"
TODO_TABLE = {"columns": ["안건", "할 일", "담당", "기한"], "rows": [
    {"m": "m4", "cells": ["배포 일정", "QA 일정표 공유", "김철수", "수요일까지"]},
    {"m": "m7", "cells": ["예산", "회의실 예약", "이영희", ""]},
    {"m": "m10", "cells": ["회식 장소", "식당 후보 알아보기", "", ""]},
]}


@pytest.fixture
def case():
    return load_case(CASE)


def task(case, name):
    return next(t for t in case["tasks"] if t["name"] == name)


def moved(answer, ref, section):
    """배치 답에서 재료 하나의 칸을 바꾼다."""
    placements = [p for p in answer["placements"] if p["m"] != ref] + [{"m": ref, "section": section}]
    return {"placements": placements, "unplaced": [u for u in answer["unplaced"] if u != ref]}


# ---- 정답표와 재료

@pytest.mark.parametrize("path", sorted(CASE.parent.glob("eval_*.json")), ids=lambda p: p.stem)
def test_사례마다_정답표는_재료와_하나씩_맞물리고_요청은_형식_검사를_통과한다(path, capsys):
    if path.stem.endswith("_request"):
        validate_request(json.loads(path.read_text(encoding="utf-8")))
        return
    assert eval_draft.dry_run(load_case(path)), capsys.readouterr().out          # then 의 정답표도 본다


def test_dry_run_은_then_의_정답표가_틀리면_알려준다(capsys):
    case = load_case(CASE.parent / "eval_mixed.json")
    then = case["tasks"][0]["then"]
    then["expect"] = [e for e in then["expect"] if e.get("text") != "다음 회의는 11월 10일"]
    assert not eval_draft.dry_run(case)
    assert "('text', '다음 회의는 11월 10일(월) 오전 10시로 정함'): 정답표에 0개" in capsys.readouterr().out


def test_정답표에_없거나_남는_항목을_알려준다(case):
    t = task(case, "배치")
    expect = [e for e in t["expect"] if e["source"] != [101, "decision", 7]] + [{"source": [999, "todo", "x"], "in": []}]
    problems = check_expect(sent_materials(t["body"]), expect)
    assert any("(101, 'decision', 7): 정답표에 0개" in p for p in problems)
    assert any("(999, 'todo', 'x')" in p and "맞는 재료 없음" in p for p in problems)


def test_다시_배치는_ai_에_보내지_않는_요약_항목을_정답표에서_뺀다(case):
    keys = {eval_draft.material_key(m) for m in sent_materials(task(case, "다시배치")["body"])}
    assert (102, "todo", "u-1") not in keys and (103, "opinion", "k3f9a1-20") not in keys
    assert ("text", "고객사 로고는 행사 전날까지 받기") in keys


def test_dry_run_은_ai_없이_맞물림만_본다(case, capsys):
    assert eval_draft.dry_run(case)
    assert "정답표와 재료가 맞물림" in capsys.readouterr().out


# ---- 채점

def test_칸이_다른_항목을_세고_어디로_갔는지_남긴다(case):
    result = make_draft(load_request(), ScriptLLM(GOOD))
    s = score(result, task(case, "배치")["expect"])
    assert (s["ok"], s["wrong"], s["serious"]) == (False, 1, 0)
    assert s["items"]['[103, "opinion", "k3f9a1-20"]'] == {"got": [NONE], "ok": False}


def test_큰_실수_칸에_들어가면_따로_센다(case):
    answer = moved(moved(GOOD, "m9", 1), "m8", 2)                      # 결정 확인 필요 → 결정 사항
    s = score(make_draft(load_request(), ScriptLLM(answer)), task(case, "배치")["expect"])
    assert (s["wrong"], s["serious"]) == (1, 1)
    assert s["items"]['[103, "decisionCheck", 12]']["got"] == ["결정 사항"]


def test_정답이_둘인_항목은_어느_쪽이든_맞다(case):
    answer = moved(moved(GOOD, "m9", 1), "m6", 3)                      # 예산 요약 → 미결 안건
    s = score(make_draft(load_request(), ScriptLLM(answer)), task(case, "배치")["expect"])
    assert s["ok"] and s["wrong"] == 0


def test_출처_없는_줄은_글로_찾는다():
    body = load_request()
    lines = previous_lines_of(make_draft(body, ScriptLLM(moved(GOOD, "m9", 1))))
    lines.append({"title": "할 일", "sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [],
                  "edited": False, "needsCheck": False, "sources": [], "draftEdited": True})
    second = dict(body, template={"items": ["결정 사항", "고객 요청"]}, previousLines=lines)
    answer = {"placements": [{"m": "m11", "section": 2}], "unplaced": [f"m{i}" for i in range(1, 11)]}
    expect = [{"text": "고객사 로고는", "in": ["할 일", "고객 요청"]}]
    s = score(make_draft(second, ScriptLLM(answer)), expect)
    assert s["ok"] and s["items"]['["text", "고객사 로고는"]']["got"] == ["고객 요청"]


def test_실패한_결과는_틀린_항목_대신_실패로_센다(case):
    result = make_draft(load_request(), ScriptLLM("JSON 아님", "JSON 아님", "JSON 아님"))
    s = score(result, task(case, "배치")["expect"])
    assert (s["ok"], s["failed"], s["wrong"]) == (False, "CHECK_FAILED", None)


def test_표는_열_개수와_행_수를_본다(case):
    t = task(case, "표")
    result = make_draft(t["body"], ScriptLLM(moved(GOOD, "m9", 1), TODO_TABLE))
    s = score(result, t["expect"], t["tables"])
    assert s["ok"] and s["tables"] == [{"field": "할 일", "columns": ["안건", "할 일", "담당", "기한"], "rows": 3, "ok": True}]
    s = score(result, t["expect"], [{"field": "할 일", "columns": 3, "rows": 3}])
    assert not s["ok"] and s["wrong"] == 0


# ---- 실행과 요약

def test_버전은_모자란_쪽이_마지막_값을_쓴다():
    assert variants_of(["v8", "v9"], ["v2"], ["v1"]) == [
        {"placement": "v8", "relayout": "v2", "table": "v1", "correction": None, "polish": None},
        {"placement": "v9", "relayout": "v2", "table": "v1", "correction": None, "polish": None}]
    assert [v["correction"] for v in variants_of(["v9"], ["v3"], ["v1"], ["v1", "v2"])] == ["v1", "v2"]


def test_판마다_돌리고_호출과_채점을_남긴다(case, tmp_path):
    case = dict(case, tasks=[task(case, "배치")])
    llm = ScriptLLM(GOOD, moved(GOOD, "m9", 1))
    out = run_case(case, llm, 2, [{"placement": "v8", "relayout": "v2", "table": "v1"}], tmp_path, progress=lambda s: None)
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["run"], r["score"]["ok"]) for r in runs] == [(1, False), (2, True)]
    assert [c["kind"] for c in runs[0]["calls"]] == ["correction", "placement", "polish"]   # 교정 → 배치 → 다듬기
    assert runs[0]["calls"][1]["input_tokens"] == 100
    assert (out / "calls.csv").read_text(encoding="utf-8").count("\n") == 7              # 머리 + 판마다 교정·배치·다듬기
    text = summarize(out)
    assert "| 배치 | placement-v8 relayout-v2 table-v1 | 1/2 | 1 | 0 | 0 | 4/8 | - | 6 |" in text     # 기준 없음 = 묶지 않음 (결정·미결만 맞음)
    assert '- 배치 / placement-v8 relayout-v2 table-v1: [103, "opinion", "k3f9a1-20"] → (넣지 않음) (1번)' in text


def test_then_은_처음_초안의_줄로_다시_배치한다(case, tmp_path):
    first = task(case, "배치")
    then = {"name": "배치-다시배치", "relayoutTo": ["결정 사항", "할 일"],
            "expect": [{"source": [101, "decision", 7], "in": ["결정 사항"]}]}
    case = dict(case, tasks=[dict(first, then=then)])
    answer = moved(GOOD, "m9", 1)
    body = load_request()
    body["previousLines"] = previous_lines_of(make_draft(body, ScriptLLM(answer)))
    refs = [m.ref for m in sent_materials(body)]                          # 다시 배치의 번호는 이전 줄 순서
    decision = next(m.ref for m in sent_materials(body) if m.kind == "decision")
    relayout = {"placements": [{"m": decision, "section": 1}], "unplaced": [r for r in refs if r != decision]}
    llm = ScriptLLM(answer, relayout)
    out = run_case(case, llm, 1, [{"placement": "v8", "relayout": "v2", "table": "v1"}], tmp_path, progress=lambda s: None)
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["task"], r["score"]["ok"]) for r in runs] == [("배치", True), ("배치-다시배치", True)]
    assert runs[1]["calls"][0]["kind"] == "relayout"


def test_묶는_기준은_칸_고르기와_따로_세고_틀린_필드를_남긴다(case, tmp_path):
    """사례 정답: 안건별 논의 안건, 결정·미결 안건 또는 묶지 않음, 할 일 담당자 또는 안건."""
    case = dict(case, tasks=[task(case, "배치")])
    good = {**moved(GOOD, "m9", 1), "grouping": {"1": "안건", "2": "묶지 않음", "4": "담당자"}}
    bad = {**moved(GOOD, "m9", 1), "grouping": {"1": "종류", "4": "담당자"}}
    out = run_case(case, ScriptLLM(good, bad), 2, [{"placement": "v8", "relayout": "v2", "table": "v1"}], tmp_path,
                   progress=lambda s: None)
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["score"]["ok"] for r in runs] == [True, True]                          # 칸 고르기는 둘 다 맞음
    assert runs[1]["score"]["grouping"]["안건별 논의"] == {"got": "종류", "ok": False}
    assert runs[1]["score"]["grouping"]["미결 안건"] == {"got": "묶지 않음", "ok": True}
    text = summarize(out)
    assert "| 2/2 | 0 | 0 | 0 | 7/8 |" in text
    assert "묶기 안건별 논의 → 종류 (1번)" in text


# ---- 오인식 교정 채점

def _stt_answer(t):
    titles = ["안건별 논의", "결정 사항", "미결 안건", "할 일"]
    return {"placements": [{"m": m.ref, "section": titles.index(e["in"][0]) + 1}
                           for m, e in zip(sent_materials(t["body"]), t["expect"])], "unplaced": []}


@pytest.fixture
def stt():
    return load_case(CASE.parent / "eval_stt.json")["tasks"][0]


def test_교정_채점은_바뀐_쌍과_바뀌면_안_되는_교정을_센다(stt):

    right = {"fixes": [{"wrong": "쿠버내티스", "right": "쿠버네티스"}, {"wrong": "프로메테오스", "right": "프로메테우스"},
                       {"wrong": "그라파냐", "right": "그라파나"}, {"wrong": "얼러트 매니져", "right": "얼러트 매니저"},
                       {"wrong": "깃헙 액선", "right": "깃허브 액션"},
                       {"wrong": "알고 시디", "right": "ArgoCD", "reading": "아르고 시디"},
                       {"wrong": "머지 리퀘스드", "right": "머지 리퀘스트"}]}
    result = make_draft(stt["body"], ScriptLLM(_stt_answer(stt), corrections=[right]))
    s = score(result, stt["expect"], fixes=stt["fixes"])
    assert s["ok"] and s["fixes"] == {"want": 7, "hit": 7, "missed": [], "extra": [], "touched": 0, "ok": True}

    partial = {"fixes": [{"wrong": "쿠버내티스", "right": "쿠버네티스"}, {"wrong": "회의실", "right": "회의록"}]}
    result = make_draft(stt["body"], ScriptLLM(_stt_answer(stt), corrections=[partial]))
    x = score(result, stt["expect"], fixes=stt["fixes"])["fixes"]
    assert (x["hit"], x["missed"], x["extra"], x["ok"]) == (1, ["프로메테오스", "그라파냐", "얼러트 매니져", "깃헙 액선", "알고 시디", "머지 리퀘스드"], ["회의록"], False)
    assert x["touched"] == 0                                    # 사람이 고친 의견에는 붙지 않는다


def test_교정_사례는_정답표와_재료가_맞물린다(stt):
    assert check_expect(sent_materials(stt["body"]), stt["expect"]) == []


def test_낱말마다_나눠_고친_교정도_맞음으로_센다(stt):
    split = {"fixes": [{"wrong": "깃헙", "right": "깃허브"}, {"wrong": "액선", "right": "액션"}]}
    result = make_draft(stt["body"], ScriptLLM(_stt_answer(stt), corrections=[split]))
    x = score(result, stt["expect"], fixes=stt["fixes"])["fixes"]
    assert x["hit"] == 1 and x["extra"] == [] and "깃헙 액선" not in x["missed"]


def test_바뀌어도_되는_말은_바뀌어도_틀림으로_세지_않는다(stt):
    guess = {"fixes": [{"wrong": "젠킨수", "right": "젠킨스"}, {"wrong": "회의실", "right": "회의록"}]}
    result = make_draft(stt["body"], ScriptLLM(_stt_answer(stt), corrections=[guess]))
    assert score(result, stt["expect"], fixes=stt["fixes"])["fixes"]["extra"] == ["회의록"]     # 젠킨스는 봐준다
