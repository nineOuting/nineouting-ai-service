"""초안 문장 다듬기 시험 (명세 7.8, SCRUM-218). 합성 요청: fixtures/draft_request.json

재료 (GOOD 배치: m9 는 넣지 않음)
  m1 결정 · m2 요약 · m3 의견 · m4 할 일(김철수, 수요일까지)         배포 일정
  m5 미결 · m6 요약(사람이 고침) · m7 할 일(사람이 고침)            예산
  m8 결정 확인 필요 · m9 의견 · m10 할 일(담당자 미정, …, 할 일 확인 필요)  회식 장소
"""
import logging

import pytest

from minutes.draft import SUCCEEDED, make_draft
from minutes.materials import build_materials
from minutes.polish import (POLISH_ERROR, check_line, digit_values, enabled_from_env, korean_values, normalize,
                            polish, read_lines, split, _hint)
from minutes_fakes import GOOD, ScriptLLM, load_request, previous_lines_of
from shared.llm import LLMError, LLMTimeout

PLACED = {"m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m10"}
GOOD_LINES = {"lines": [
    {"m": "m1", "text": "1차 배포를 10월 30일(목)로 정함."},
    {"m": "m2", "text": "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 있음."},
    {"m": "m3", "text": "목요일은 QA 일정과 겹쳐 부담이 된다는 우려가 있음."},
    {"m": "m4", "text": "QA 일정표 공유하기"},
    {"m": "m5", "text": "월 사용량을 확인하기 전이라 승인 여부를 정하지 못함."},
    {"m": "m8", "text": "강남역 근처로 정함."},
    {"m": "m10", "text": "식당 후보 알아보기"},
]}


@pytest.fixture
def body():
    return load_request()


@pytest.fixture
def materials(body):
    return build_materials(body["agendas"], body["summary"])


def by_ref(materials):
    return {m.ref: m for m in materials}


def line_texts(result):
    return {line["sources"][0]["id"] or line["sources"][0]["decisionPointId"]: line["sentence"]
            for s in result["sections"] for line in s["evidence"]}


# ---- ① 정리

@pytest.mark.parametrize("before, after", [
    ("처음 출시일은 「십이월 사일」(12월 4일로 읽힘)이었음", "처음 출시일은 12월 4일이었음"),
    ("오류가 난 항목 「세 개」(3개로 읽힘)를 고침", "오류가 난 항목 3개를 고침"),
    ("「일주일」(7일로 읽힘) 정도 필요", "7일 정도 필요"),
    ("가입자가 「사백 명쯤」(약 400명으로 읽힘) 늘었음", "가입자가 약 400명 늘었음"),
    ("「아침 아홉시 이 호선 지옥 같고」(아침 9시, 2호선으로 읽힘)라는 말", "「아침 아홉시 이 호선 지옥 같고」(아침 9시, 2호선으로 읽힘)라는 말"),
    ("「[테라폼]」 모듈은 다음 주에 정리", "[테라폼] 모듈은 다음 주에 정리"),
    ("「[테라폼] 모듈」을 정리", "「[테라폼] 모듈」을 정리"),
    ("「헬륨 셔츠」도 누군가 해야 함", "「헬륨 셔츠」도 누군가 해야 함"),
    ("「삼백만 원」을 올렸고(3,000,000원으로 읽힘)", "「삼백만 원」을 올렸고(3,000,000원으로 읽힘)"),
])
def test_읽은_값이_인용을_다_설명할_때만_바꾼다(before, after):
    assert normalize(before) == after


# ---- 숫자

def test_아라비아_숫자는_쉼표를_떼고_만_억을_곱하고_대괄호_안도_센다():
    assert digit_values("300만 원과 3,000,000원, 1.5배, [쿠버네티스 2?] 12월") == {3_000_000.0, 1.5, 2.0, 12.0}


@pytest.mark.parametrize("text, value", [
    ("십이월", 12), ("십일일", 11), ("삼백만 원", 3_000_000), ("세 건", 3), ("열두 시", 12), ("스물다섯", 25),
    ("일주일", 7), ("일주일", 1), ("이 주 정도", 2), ("한 다섯 개", 5), ("아홉시", 9),
])
def test_한글_수_낱말로_읽을_수_있는_값(text, value):
    assert float(value) in korean_values(text)


def test_띄어_쓴_한자_수도_읽는다():
    assert 11.0 in korean_values("「십 일월 중순에서 말 사이」")


def test_불확실_표시_안의_수_낱말을_숫자로_바꾼_것은_받는다():
    before = "공통 모델에 「약을 팔 한 여섯 개」(6개로 읽힘)를 넣으면 끝이라는 설명이 나옴"
    assert check_line(before, "공통 모델에 [약을 팔 6개?]를 넣으면 끝이라는 설명이 있음.", before) is None
    assert check_line(before, "공통 모델에 [약을 먹 6개?]를 넣으면 끝이라는 설명이 있음.", before) == "marks"
    assert check_line(before, "공통 모델에 [약을 팔 한 여섯 개?]를 넣으면 끝이라는 설명이 있음.", before) is None   # 한글 수로 남음


def test_수가_아닌_한자_나열은_읽지_않는다():
    assert 41.0 not in korean_values("사일")


# ---- ③ 검사

BEFORE = "처음 출시일은 12월 4일이었으나 결제 연동 시험이 끝나지 않아 빠듯하고, 「세 개」를 고쳐 다시 시험하는 데 [쿠버네티스] 정리가 필요하다는 말이 나옴."
ORIGINAL = "처음 출시일은 「십이월 사일」(12월 4일로 읽힘)이었으나 결제 연동 시험이 끝나지 않아 빠듯하고, 「세 개」를 고쳐 다시 시험하는 데 [쿠버네티스] 정리가 필요하다는 말이 나옴."


@pytest.mark.parametrize("after, problem", [
    ("처음 출시일은 12월 4일이었으나 결제 연동 시험 미완료로 빠듯함. 3개를 고쳐 재시험하는 데 [쿠버네티스] 정리가 필요함.", None),
    ("처음 출시일은 12월 4일이었으나 빠듯함. [세 개?]를 고쳐 다시 시험하는 데 [쿠버네티스] 정리가 필요함.", None),
    ("처음 출시일은 12월 5일이었으나 빠듯함. 3개를 고쳐 다시 시험하는 데 [쿠버네티스] 정리 필요함.", "numbers"),   # 없던 숫자
    ("처음 출시일이 빠듯함. 3개를 고쳐 다시 시험하는 데 [쿠버네티스] 정리가 필요함.", "numbers"),               # 12월 4일 빠짐
    ("처음 출시일은 12월 4일이었으나 빠듯함. 3개를 고쳐 다시 시험하는 데 쿠버네티스 정리가 필요함.", "marks"),    # 교정 표시 지움
    ("처음 출시일은 12월 4일이었으나 빠듯함. 3개를 [고쳐?] 다시 시험하는 데 [쿠버네티스] 정리가 필요함.", "marks"),  # 인용에 없던 말
    ("처음 출시일은 12월 4일이었으나 빠듯함. 「세 개」를 고쳐 다시 시험하는 데 [쿠버네티스] 정리 필요함.", "leftover"),
    ("12월 4일 [쿠버네티스] 3개", "length"),
    ("", "shape"),
    ("첫 줄 12월 4일 [쿠버네티스] 3개\n둘째 줄", "shape"),
])
def test_검사(after, problem):
    assert check_line(BEFORE, after, ORIGINAL) == problem


@pytest.mark.parametrize("before, after, problem", [
    ("「에이더블유에스」를 맡아 해보기", "AWS를 맡아 해보기", "latin"),
    ("유저 플로우 아이에이 기능 명세서는 다 만들었다는 말이 나옴", "유저 플로우 AI 기능 명세서는 다 만들었음.", "latin"),
    ("Terraform 은 프로젝트 시간에 공부한다고 했습니다", "Terraform 은 프로젝트 시간에 공부하기로 함.", None),
])
def test_원문에_없던_영어_낱말이_생기면_걸린다(before, after, problem):
    assert check_line(before, after, before) == problem


@pytest.mark.parametrize("before, after, problem", [
    ("「크로노빔」을 더 공부하겠다는 말이 나옴", "크로노빔을 더 공부하겠다고 함.", "quote"),          # 불확실한 말이 확실한 말처럼 남음
    ("「크로노빔」을 더 공부하겠다는 말이 나옴", "[크로노빔?]을 더 공부하기로 함.", None),
    ("「달빛 파이프」 하기", "달빛 파이프 하기", "quote"),
    ("「[AWS] 클린」이라는 표현이 나옴", "[AWS] 클린도 나옴.", "quote"),                       # 교정 표시 밖 글이 표시 없이 남음
    ("「[AWS] 클린」이라는 표현이 나옴", "[AWS] [클린?]도 나옴.", None),
    ("모임은 금요일로 하자는 말에 「좋은데요」라고 답했습니다", "모임을 금요일로 하는 것을 제안함. 좋음.", None),                                        # 사람 말은 풀어 쓴다
    ("「십 일월 중순에서 말 사이」까지 답하기로 했다", "11월 중순에서 말 사이까지 답하기로 함.", None),  # 수 인용은 숫자로
    ("모임은 「금요일」에 하기로 했다", "모임은 금요일에 하기로 함.", None),                         # 날짜 낱말
    ("「크로노빔」은 STT 불확실함. 공부하겠다는 말이 나옴", "[크로노빔?]은 의미가 불확실함. 공부하기로 함.", None),  # 남은 설명 말은 거르지 않는다
    ("기획 분들이 [기능 명세]에서 화면 흐름을 다 만들었다는 말이 나옴; [기능 명세]는 STT 불확실함.",
     "기획 분들이 [기능 명세]에서 화면 흐름을 다 만들었음.", None),  # 설명 안의 표시는 같이 지워도 된다
    ("[기능 명세]에서 만들었고 [기능 명세]를 고친다는 말이 나옴", "[기능 명세]에서 만들었다고 함. 고친다고 함.", "marks"),
    ("결과가 불확실하다는 우려가 나옴", "결과가 불확실함.", None),                       # 원래 글의 불확실은 남는다
])
def test_불확실한_인용을_표시_없이_두면_걸린다(before, after, problem):
    assert check_line(before, after, before) == problem


@pytest.mark.parametrize("before, after, problem", [
    ("일 더블유에스를 맡아 해보기", "일 더블유스를 맡아 해보기", "words"),                          # 글자를 뺌
    ("개발 기간은 이 주라는 예상이 나왔고 초대 할라고 메일을 올렸다는 말이 나옴",
     "개발 기간은 2주로 예상되고 초대하려고 메일을 올렸음.", None),                           # 말끝만 바꿈
    ("이동 시간이 길다는 언급이 이어짐", "이동 시간이 긴 것을 언급함.", None),
    ("위험 부담 때문에 맡기기 어렵고 자리가 적다는 말이 나옴", "위험 부담 때문에 맡기 어렵고 자리가 적다고 함.", "words"),  # 뜻이 바뀜
    ("계정은 만들었지만 남았는지는 모른다고 했습니다", "계정은 만들었으나 남았는지는 모름.", None),
])
def test_원문_낱말의_표기가_바뀌면_걸린다(before, after, problem):
    assert check_line(before, after, before) == problem


def test_만_단위로_바꿔_쓴_숫자도_같은_값으로_본다():
    before = "이번 달 예산 「삼백만 원」을 올렸고 지난달에는 2,000,000원을 썼다는 말이 나옴"
    assert check_line(before, "이번 달 예산 300만 원을 올렸고 지난달에는 200만 원을 썼음.", before) is None


# ---- 할 일·결정 확인 필요는 글만 보낸다

def test_할_일은_담당자와_기한을_떼고_결정_확인_필요는_사항_문구와_틀을_뗀다(materials):
    ms = by_ref(materials)
    assert split(ms["m4"]) == ("", "QA 일정표 공유", " (김철수, 수요일까지)")
    assert split(ms["m10"]) == ("", "식당 후보 알아보기", " (담당자 미정, 기한 미정, 할 일 확인 필요)")
    assert split(ms["m8"]) == ("회식 장소 확정 (결정 확인 필요: ", "강남역 근처로 정함", ")")
    assert split(ms["m2"]) == ("", ms["m2"].text, "")


def test_답_앞에_따라_쓴_번호와_종류는_뗀다(materials):
    reply = {"lines": [{"m": "m1", "text": "[결정] 1차 배포를 10월 30일(목)로 정함."},
                       {"m": "m2", "text": "m2 | 요약 | 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 있음."},
                       {"m": "m4", "text": "할 일 | QA 일정표 공유하기"}]}
    p = polish(materials, {"m1", "m2", "m4"}, ScriptLLM(polishes=[reply]), 10)
    ms = by_ref(p.materials)
    assert p.polished == 3 and ms["m1"].text == "1차 배포를 10월 30일(목)로 정함."
    assert ms["m4"].text == "QA 일정표 공유하기 (김철수, 수요일까지)"


def test_답에서_모양이_맞는_줄만_읽는다():
    data = {"lines": [{"m": "m1", "text": "가"}, {"m": "m1", "text": "나"}, {"m": "m2"}, {"m": 3, "text": "다"}, "m4"]}
    assert read_lines(data) == ({"m1": "가"}, [])
    assert read_lines({"fixes": []})[0] is None


# ---- 다듬기

def test_통과한_줄은_다듬은_글이_되고_떼어_낸_부분은_코드가_다시_붙인다(materials):
    p = polish(materials, PLACED, ScriptLLM(polishes=[GOOD_LINES]), 10)
    ms = by_ref(p.materials)
    assert ms["m4"].text == "QA 일정표 공유하기 (김철수, 수요일까지)"
    assert ms["m8"].text == "회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함.)"
    assert ms["m10"].text == "식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)"
    assert (p.sent, p.polished, p.failed, p.error_code) == (7, 7, {}, None)


def test_사람이_고친_재료와_넣지_못한_재료는_보내지_않는다(materials):
    llm = ScriptLLM(polishes=[GOOD_LINES])
    p = polish(materials, PLACED, llm, 10)
    sent = llm.polish_calls[0][1]
    assert "m6 " not in sent and "m7 " not in sent and "m9 " not in sent
    assert by_ref(p.materials)["m6"] is by_ref(materials)["m6"]
    assert "m4 | 할 일 | QA 일정표 공유" in sent and "김철수" not in sent        # 담당자는 보내지 않는다


def test_검사에_걸리거나_답에_없는_줄은_정리한_글을_쓴다(materials):
    reply = {"lines": [l if l["m"] != "m1" else {"m": "m1", "text": "1차 배포를 11월 30일로 정함."}
                       for l in GOOD_LINES["lines"] if l["m"] != "m2"]}
    llm = ScriptLLM(polishes=[reply])                     # 다시 물을 때는 빈 답
    p = polish(materials, PLACED, llm, 10)
    ms, before = by_ref(p.materials), by_ref(materials)
    assert ms["m1"].text == before["m1"].text and ms["m2"].text == before["m2"].text
    assert p.failed == {"missing": 2} and p.polished == 5 and p.retried == 2 and len(llm.polish_calls) == 2


def test_걸린_줄만_이유를_붙여_한_번_더_보내고_통과하면_다듬은_글을_쓴다(materials):
    first = {"lines": [l if l["m"] != "m1" else {"m": "m1", "text": "1차 배포를 11월 30일로 정함."}
                       for l in GOOD_LINES["lines"]]}
    again = {"lines": [l for l in GOOD_LINES["lines"] if l["m"] == "m1"]}
    llm = ScriptLLM(polishes=[first, again])
    p = polish(materials, PLACED, llm, 10)
    retry = llm.polish_calls[1][1]
    assert "m1 | " in retry and "m3 | " not in retry                       # 걸린 줄만 보낸다
    assert "<고칠 점>" in retry and "m1: 글에 있는 숫자를 모두 남기고" in retry
    good = next(l["text"] for l in GOOD_LINES["lines"] if l["m"] == "m1")
    assert by_ref(p.materials)["m1"].text == good
    assert p.failed == {} and p.polished == 7 and p.retried == 1 and p.attempts == 2


def test_다시_묻다_실패해도_첫_답에서_통과한_줄은_다듬은_글을_쓴다(materials):
    first = {"lines": [l for l in GOOD_LINES["lines"] if l["m"] != "m2"]}
    llm = ScriptLLM(polishes=[first, LLMTimeout("t"), LLMTimeout("t")])
    p = polish(materials, PLACED, llm, 10)
    assert p.polished == 6 and p.error_code is None and p.failed == {"missing": 1}


@pytest.mark.parametrize("reply, code", [
    (LLMTimeout("t"), "LLM_TIMEOUT"), (LLMError("e"), "LLM_FAILED"), ("JSON 아님", "CHECK_FAILED")])
def test_다듬기가_실패하면_모든_줄을_정리한_글로(materials, reply, code):
    p = polish(materials, PLACED, ScriptLLM(polishes=[reply, reply]), 10)
    assert p.error_code == code and [m.text for m in p.materials] == [m.text for m in materials]


def test_다듬기_코드에서_예외가_나도_정리한_글로(materials, monkeypatch):
    import minutes.polish as mod
    monkeypatch.setattr(mod, "build_user", lambda chosen, before: 1 / 0)
    assert polish(materials, PLACED, ScriptLLM(), 10).error_code == POLISH_ERROR


def test_끄면_AI를_부르지_않고_정리만_한다(body):
    body["summary"]["agendas"][0]["summary"][0]["text"] = "「십이월 사일」(12월 4일로 읽힘)로 하자는 제안이 나옴"
    ms = build_materials(body["agendas"], body["summary"])
    llm = ScriptLLM()
    p = polish(ms, PLACED, llm, 10, enabled=False)
    assert llm.polish_calls == [] and by_ref(p.materials)["m2"].text == "12월 4일로 하자는 제안이 나옴"


# ---- 초안 흐름

def test_초안의_줄_글과_칸_글에_다듬은_글이_들어간다(body):
    result = make_draft(body, ScriptLLM(GOOD, polishes=[GOOD_LINES]))
    assert result["status"] == SUCCEEDED
    texts = line_texts(result)
    assert texts["k3f9a1-10"] == "배포 일정: QA 일정표 공유하기 (김철수, 수요일까지)"
    contents = "\n".join(s["content"] for s in result["sections"])
    assert "1차 배포를 10월 30일(목)로 정함." in contents and "하자는 제안이 있음." in contents


TODO_TABLE = {"columns": ["안건", "할 일", "담당", "기한"], "rows": [
    {"m": "m4", "cells": ["배포 일정", "QA 일정표 공유", "김철수", "수요일까지"]},
    {"m": "m7", "cells": ["예산", "회의실 예약", "이영희", ""]},
    {"m": "m10", "cells": ["회식 장소", "식당 후보 알아보기", "", ""]},
]}


def test_표_필드에_놓인_재료는_다듬지_않는다(body):
    body["template"] = {"fields": [{"title": t, "type": "table" if t == "할 일" else "text", "aiFill": True}
                                   for t in ["안건별 논의", "결정 사항", "미결 안건", "할 일"]]}
    llm = ScriptLLM(GOOD, TODO_TABLE, polishes=[GOOD_LINES])
    make_draft(body, llm)
    sent = llm.polish_calls[0][1]
    assert "m4 " not in sent and "m10 " not in sent and "m1 " in sent


def test_다시_배치에서는_다듬지_않고_이전_줄_글을_쓴다(body):
    first = make_draft(body, ScriptLLM(GOOD, polishes=[GOOD_LINES]))
    again = dict(body, previousLines=previous_lines_of(first), template={"items": ["결정 사항", "할 일"]})
    answer = {"placements": [{"m": f"m{i}", "section": 1} for i in range(1, 10)], "unplaced": []}
    llm = ScriptLLM(answer)
    result = make_draft(again, llm)
    assert llm.polish_calls == []
    assert "배포 일정: 1차 배포를 10월 30일(목)로 정함." in [l["sentence"] for l in result["sections"][0]["evidence"]]


def test_끄면_다듬지_않는다(body):
    llm = ScriptLLM(GOOD, polishes=[GOOD_LINES])
    result = make_draft(body, llm, polish=False)
    assert llm.polish_calls == [] and line_texts(result)["k3f9a1-10"] == "배포 일정: QA 일정표 공유 (김철수, 수요일까지)"


def test_로그에는_다듬기_개수만_남는다(body, caplog):
    reply = {"lines": [l for l in GOOD_LINES["lines"] if l["m"] != "m2"]}
    with caplog.at_level(logging.INFO, logger="minutes.draft"):
        make_draft(body, ScriptLLM(GOOD, polishes=[reply]))
    log = caplog.text
    assert "polish_prompt=polish-v1" in log and "polish_sent=7 polish_done=6" in log and "polish_failed=missing:1" in log
    assert "배포" not in log and "QA" not in log


@pytest.mark.parametrize("env, on", [({}, True), ({"MINUTES_POLISH": "1"}, True), ({"MINUTES_POLISH": "0"}, False)])
def test_다듬기는_기본으로_켜고_0이면_끈다(env, on):
    assert enabled_from_env(env) is on


@pytest.mark.parametrize("raw", ["2", "off", "false"])
def test_다듬기_설정이_0이나_1이_아니면_설정_오류(raw):
    with pytest.raises(ValueError):
        enabled_from_env({"MINUTES_POLISH": raw})



# ---- 다시 물을 때 알려 주는 이유

def test_대괄호를_겹치면_표시_검사에_걸린다():
    before = "「도쿄에서 바꿀 때 [Helm 차트화]」로 일괄적으로 했다는 말이 나옴"
    assert check_line(before, "[도쿄에서 바꿀 때 [Helm 차트화]?]로 일괄적으로 함.", before) == "marks"
    assert check_line(before, "[도쿄에서 바꿀 때?] [Helm 차트화]로 일괄적으로 함.", before) is None


@pytest.mark.parametrize("before, after, problem, hint", [
    ("기획 분들이 [요구사항 문서]에서 화면 흐름을 다 만들었다는 말이 나옴",
     "기획 분들이 [요구사항 문서?]에서 화면 흐름을 다 만들었다고 함.", "marks", "[요구사항 문서] 는 바로잡은 말이라 글자 그대로 둘 것"),
    ("「크로노빔」을 더 공부하겠다는 말이 나옴", "[크로노빔]을 더 공부하겠다고 함.", "marks", "[크로노빔] 처럼 물음표 없는 대괄호를"),
    ("위험 부담 때문에 맡기기 어렵고 자리가 적다는 말이 나옴", "위험 부담 때문에 맡기 어렵고 자리가 적다고 함.", "words", "\"맡기기 어렵고 → 맡기 어렵고\""),
    ("일 더블유에스를 맡아 해보기", "일 더블유스를 맡아 해보기", "words", "\"더블유에스를 → 더블유스를\""),
    ("「크로노빔」을 더 공부하겠다는 말이 나옴", "크로노빔을 더 공부하겠다고 함.", "quote", "[크로노빔?] 로 쓸 것"),
    ("「[AWS] 클린」이라는 표현이 나옴", "[AWS] 클린도 나옴.", "quote", "[클린?] 로 쓸 것"),
])
def test_다시_물을_때_걸린_말을_짚어_준다(before, after, problem, hint):
    assert check_line(before, after, before) == problem
    assert hint in _hint(problem, before, after, before)


@pytest.mark.parametrize("after, problem", [
    ("얼마나 걸릴지 모른다고 우려함.", "reported"),
    ("그때 가서 일이 남는 분에게 주겠다고 함.", "reported"),
    ("금요일에 보자고 제안함.", "reported"),
    ("기획 문서를 봤냐고 물음.", "reported"),
    ("공부한다고 했고 정리하기로 함.", "reported"),
    ("얼마나 걸릴지 모름.", None),
    ("그때 가서 일이 남는 사람에게 주기로 함.", None),
    ("금요일에 보는 것을 제안함.", None),
    ("기획 문서를 봤는지 물음. 아직 못 봄.", None),
    ("[팬션 차트 작성 해 보셨나요?]라고 물음.", None),     # 알아듣지 못한 말은 옮길 수밖에 없다
    ("[크로노빔?]이라고 함.", None),
    ("일정이 빠듯하다고 해서 미룸.", None),                  # 이유 말
    ("2주 정도면 나올 것으로 봄.", None),
])
def test_남의_말을_옮기는_꼴이_남으면_걸린다(after, problem):
    assert check_line(after, after, after) == problem


def test_옮기는_꼴은_걸린_말과_바로_쓰는_예를_알려_준다():
    after = "얼마나 걸릴지 모른다고 우려함."
    hint = _hint("reported", after, after, after)
    assert "\"모른다고 우려함\"" in hint and "주기로 함" in hint and "~는지 물음" in hint
