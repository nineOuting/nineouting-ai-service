import copy
import json
from pathlib import Path

import pytest

from rag.errors import INTERNAL_ERROR, NO_TEXT, IngestError
from rag.loaders.minutes import check_format, load_minutes

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


# 확정 회의록 형식 (회의록 초안 명세 4.1). 합성 예시: 칸 5개 중 "미결 안건"은 해당 내용 없음, "고객 요청"은 사용자가 추가한 칸
CONFIRMED = json.loads((Path(__file__).parent / "fixtures" / "minutes_confirmed.json").read_text(encoding="utf-8"))


@pytest.fixture
def confirmed():
    return copy.deepcopy(CONFIRMED)


def test_확정_회의록은_칸의_줄_하나가_조각_하나이고_근거_위치는_칸_이름(confirmed):
    r = load_minutes(confirmed)
    assert [(p.context, p.content) for p in r.pieces] == [
        ("안건별 논의", "배포 일정: 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴"),
        ("안건별 논의", "배포 일정: 목요일은 QA 일정과 겹쳐 부담이 된다는 우려"),
        ("안건별 논의", "예산: 광고 예산은 월 사용량을 확인한 뒤 정하기로 함"),
        ("결정 사항", "배포 일정: 1차 배포를 10월 30일(목)로 정했다."),
        ("할 일", "배포 일정: QA 일정표 공유 (김철수, 수요일까지)"),          # "* " 를 뗌
        ("할 일", "예산: 회의실 예약 (이영희, 기한 미정)"),                    # "• " 를 뗌
        ("고객 요청", "고객사 로고는 행사 전날까지 받기"),                     # 표시 없는 줄, 사용자가 추가한 칸
    ]
    assert r.skipped == 1                                                     # "해당 내용 없음"
    assert r.pieces[3].location == {"type": "section", "heading": "결정 사항", "label": "결정 사항"}
    assert {p.kind for p in r.pieces} == {"section"}


def test_확정_회의록에_넣을_줄이_없으면_no_text(confirmed):
    confirmed["sections"] = [{"title": "결정 사항", "content": "해당 내용 없음"}, {"title": "할 일", "content": " \n"}]
    with pytest.raises(IngestError) as e:
        load_minutes(confirmed)
    assert e.value.code == NO_TEXT


def test_형식_이름을_돌려준다(confirmed, minutes):
    assert check_format(confirmed) == "1.0" and check_format(minutes) == "0.1"


@pytest.mark.parametrize("break_it, word", [
    (lambda m: m.update(formatVersion="2.0"), "formatVersion 이 1.0 도"),
    (lambda m: m.update(formatVersion=1.0), "formatVersion 이 1.0 도"),
    (lambda m: m.pop("meetingId"), "meetingId"),
    (lambda m: m.update(meetingId="31"), "meetingId"),
    (lambda m: m.update(meetingStartAt=""), "meetingStartAt"),
    (lambda m: m.update(confirmedAt=20261021), "confirmedAt"),
    (lambda m: m.update(savedAt=20261021), "savedAt"),
    (lambda m: m.update(header=["머리글"]), "header 가 문자열"),
    (lambda m: m.update(footer=1), "footer 가 문자열"),
    (lambda m: m.update(sections={"title": "x"}), "sections 가 배열"),
    (lambda m: m["sections"].append("결정 사항"), "sections[5] 가 객체"),
    (lambda m: m["sections"][1].update(title=" "), "sections[1].title"),
    (lambda m: m["sections"][1].update(content=None), "sections[1].content"),
])
def test_확정_회의록_형식이_틀리면_칸_위치를_담은_internal_error(confirmed, break_it, word):
    break_it(confirmed)
    with pytest.raises(IngestError) as e:
        check_format(confirmed)
    assert e.value.code == INTERNAL_ERROR and word in str(e.value)
    assert "배포" not in str(e.value)                                         # 회의록 글은 오류에 안 남음


# 표·머리글·바닥글이 있는 확정 회의록 (명세 4.1, SCRUM-100). AI 가 채운 표("할 일")와 사용자가 만든 표("예산표")
TABLED = json.loads((Path(__file__).parent / "fixtures" / "minutes_confirmed_table.json").read_text(encoding="utf-8"))


def test_표는_행마다_조각_하나이고_열_이름을_붙이며_빈_칸은_뺀다():
    pieces = load_minutes(copy.deepcopy(TABLED)).pieces
    todo = [p.content for p in pieces if p.context == "할 일"]
    assert todo == [
        "안건: 배포 일정 | 할 일: QA 일정표 공유 | 담당자: 김철수 | 기한: 수요일까지",
        "안건: 예산 | 할 일: 회의실 예약 | 담당자: 이영희",
        "안건: 회식 장소 | 할 일: 식당 후보 알아보기",
    ]
    assert all(p.location == {"type": "section", "heading": "할 일", "label": "할 일"} for p in pieces if p.context == "할 일")


def test_표_앞뒤의_글은_줄마다_조각이고_칸_안의_막대는_칸을_나누지_않는다():
    budget = [p.content for p in load_minutes(copy.deepcopy(TABLED)).pieces if p.context == "예산표"]
    assert budget == ["아래는 사용자가 직접 만든 표",
                      "항목: 홍보 | 금액: 2,000,000원 | 비고: 온라인 광고 | 전단",      # 정렬 표시 :--- 도 구분 줄
                      "항목: 다과 | 금액: 300,000원"]


def test_머리글과_바닥글은_각각_조각_하나이고_위치는_머리글_바닥글():
    pieces = load_minutes(copy.deepcopy(TABLED)).pieces
    assert (pieces[0].content, pieces[0].context) == ("고객성공팀 주간 회의 2026년 10월 셋째 주", "머리글")   # 여러 줄은 이음
    assert pieces[0].location == {"type": "section", "heading": "머리글", "label": "머리글"}
    assert (pieces[-1].content, pieces[-1].location["heading"]) == ("사내 공유용 (외부 전달 금지)", "바닥글")
    assert len(pieces) == 1 + 1 + 3 + 3 + 1 + 1                               # 머리글, 결정, 할 일 3행, 예산표 3줄, 메모, 바닥글


@pytest.mark.parametrize("header, footer", [(None, None), ("", "  \n "), (None, "사내 공유용")])
def test_머리글_바닥글이_없거나_비면_넣지_않는다(header, footer):
    m = dict(copy.deepcopy(TABLED), header=header, footer=footer)
    headings = [p.location["heading"] for p in load_minutes(m).pieces]
    assert "머리글" not in headings and headings.count("바닥글") == (1 if footer and footer.strip() else 0)


@pytest.mark.parametrize("content, lines", [
    ("| 항목 | 금액 |\n|---|---|", ["항목 | 금액"]),                                        # 몸통 행 없음
    ("| 항목 | 금액 |\n|---|---|\n|  |  |", ["항목 | 금액"]),                              # 빈 행뿐
    ("| 막대로 시작하지만 표가 아닌 줄\n둘째 줄", ["| 막대로 시작하지만 표가 아닌 줄", "둘째 줄"]),
    ("항목 | 금액\n---|---\n홍보 | 2만 원", ["항목 | 금액", "---|---", "홍보 | 2만 원"]),     # 막대로 시작하지 않으면 표로 보지 않음
    ("| a | b |\n|---|---|\n| 1 | 2 | 3 |", ["a: 1 | b: 2 | 3"]),                          # 머리 행보다 칸이 많음
])
def test_표_경계(content, lines):
    m = dict(copy.deepcopy(TABLED), header=None, footer=None, sections=[{"title": "표", "content": content}])
    assert [p.content for p in load_minutes(m).pieces] == lines
