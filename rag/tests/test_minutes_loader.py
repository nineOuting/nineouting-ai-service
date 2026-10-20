import copy
import json
from pathlib import Path

import pytest

from rag.errors import INTERNAL_ERROR, NO_TEXT, IngestError
from rag.loaders.minutes import load_minutes

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "minutes_basic.json").read_text(encoding="utf-8"))


@pytest.fixture
def minutes():
    return copy.deepcopy(FIXTURE)


def test_항목마다_조각_하나_초를_밀리초로(minutes):
    r = load_minutes(minutes)
    assert len(r.pieces) == 4
    assert r.skipped == 2                      # 공백뿐인 글 1, 근거 없음 1
    p = r.pieces[0]
    assert p.content == "다음 스프린트부터 일정 관리는 Jira로 옮기기로 함"
    assert p.context == "도구"
    assert p.kind == "summary"
    assert p.location == {"type": "time", "startMs": 312500, "endMs": 340000, "label": "05:12~05:40"}


def test_근거_구간이_여럿이면_전체_범위와_spans(minutes):
    loc = load_minutes(minutes).pieces[1].location
    assert loc["startMs"] == 600040 and loc["endMs"] == 921300
    assert loc["label"] == "10:00~10:15 외 1곳"           # 시작 순으로 정렬한 첫 구간
    assert loc["spans"] == [{"startMs": 600040, "endMs": 615000}, {"startMs": 905000, "endMs": 921300}]


def test_한_시간이_넘으면_시까지_표시(minutes):
    p = load_minutes(minutes).pieces[2]
    assert p.location["label"] == "1:02:03~1:02:10"
    assert p.context is None                               # 빈 주제는 맥락 없음


def test_decisions_항목도_넣음(minutes):
    p = load_minutes(minutes).pieces[3]
    assert p.kind == "decisions"
    assert p.location["startMs"] == 1200000


def test_넣을_항목이_없으면_no_text(minutes):
    minutes["summary"] = [{"text": "", "evidence": [{"start": 1, "end": 2}]}]
    minutes["decisions"] = []
    with pytest.raises(IngestError) as e:
        load_minutes(minutes)
    assert e.value.code == NO_TEXT


@pytest.mark.parametrize("break_it", [
    lambda m: m.update(schema_version="0.2"),
    lambda m: m.pop("summary"),
    lambda m: m.update(decisions={"text": "x"}),
    lambda m: m["summary"].append("문자열 항목"),
    lambda m: m["summary"][0].update(text=123),
    lambda m: m["summary"][0].update(evidence={"start": 1, "end": 2}),
    lambda m: m["summary"][0]["evidence"][0].update(start="312"),
    lambda m: m["summary"][0]["evidence"][0].update(start=True),
    lambda m: m["summary"][0]["evidence"][0].update(start=-1),
    lambda m: m["summary"][0]["evidence"][0].update(start=float("nan")),
    lambda m: m["summary"][0]["evidence"][0].update(start=400, end=300),
])
def test_형식이_틀리면_internal_error_이고_글은_오류에_안_남음(minutes, break_it):
    break_it(minutes)
    with pytest.raises(IngestError) as e:
        load_minutes(minutes)
    assert e.value.code == INTERNAL_ERROR
    assert "Jira" not in str(e.value) and "스프린트" not in str(e.value)


def test_객체가_아니면_internal_error():
    with pytest.raises(IngestError) as e:
        load_minutes([])
    assert e.value.code == INTERNAL_ERROR
