"""배치 결과 → sections 시험. 배치는 AI 대신 손으로 정해 넣는다."""
import json
from pathlib import Path

import pytest

from minutes.lines import EMPTY_SECTION, build_sections, content_of
from minutes.materials import build_materials
from minutes.models import Placement

FIXTURE = Path(__file__).parent / "fixtures" / "draft_request.json"
ITEMS = ["안건별 논의", "결정 사항", "미결 안건", "할 일", "고객 요청"]

# 칸 위치: 0 안건별 논의, 1 결정 사항, 2 미결 안건, 3 할 일, 4 고객 요청 (빈 칸)
SECTIONS = {"m1": 1, "m2": 0, "m3": 0, "m4": 3, "m5": 2, "m6": 0, "m7": 3, "m8": 2, "m10": 3}
TEXTS = {"m1": "1차 배포를 10월 30일(목)로 정했다."}             # 다듬기 재료는 결정뿐


@pytest.fixture
def materials():
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return build_materials(body["agendas"], body["summary"])


def placement(sections=None, texts=None, unplaced=("m9",)):
    return Placement(sections=dict(sections or SECTIONS), texts=dict(texts or TEXTS), unplaced=tuple(unplaced))


def section(sections, title):
    return next(s for s in sections if s["title"] == title)


def test_칸은_양식_순서와_이름_그대로(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [s["title"] for s in sections] == ITEMS


def test_줄_앞에_안건_제목을_붙이고_고정_글과_AI_글을_쓴다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [line["sentence"] for line in section(sections, "할 일")["evidence"]] == [
        "배포 일정: QA 일정표 공유 (김철수, 수요일까지)",
        "예산: 회의실 예약 (이영희, 기한 미정)",
        "회식 장소: 식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)",
    ]
    assert section(sections, "결정 사항")["evidence"][0]["sentence"] == "배포 일정: 1차 배포를 10월 30일(목)로 정했다."


def test_결정할_사항이_없으면_안건_제목이_두_번_나오지_않는다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [line["sentence"] for line in section(sections, "미결 안건")["evidence"]] == [
        "예산: 광고 예산 승인",
        "회식 장소: (결정 확인 필요: 강남역 근처로 하자는 말은 있으나 합의 표현이 없음)",
    ]
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body["agendas"][1]["decisionPoint"] = None
    sections, _ = build_sections(ITEMS, build_materials(body["agendas"], body["summary"]), placement())
    assert section(sections, "미결 안건")["evidence"][0]["sentence"] == "예산"


def test_칸_안_순서는_AI_순서가_아니라_재료_순서(materials):
    shuffled = dict(reversed(list(SECTIONS.items())))
    sections, _ = build_sections(ITEMS, materials, placement(sections=shuffled))
    discussion = section(sections, "안건별 논의")["evidence"]
    assert [line["sources"][0]["id"] for line in discussion] == ["k3f9a1-1", "k3f9a1-7", "k3f9a1-3"]


def test_고정_재료는_AI가_글을_보내도_무시한다(materials):
    texts = {**TEXTS, "m4": "QA 일정표를 금요일까지 공유", "m6": "다른 글"}
    sections, _ = build_sections(ITEMS, materials, placement(texts=texts))
    sentences = [line["sentence"] for s in sections for line in s["evidence"]]
    assert "배포 일정: QA 일정표 공유 (김철수, 수요일까지)" in sentences
    assert "예산: 광고 예산은 월 사용량을 확인한 뒤 정하기로 함" in sentences
    assert not any("금요일" in s or "다른 글" in s for s in sentences)


def test_근거_표시_출처를_재료에서_옮긴다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    decision = section(sections, "결정 사항")["evidence"][0]
    assert decision == {
        "sentence": "배포 일정: 1차 배포를 10월 30일(목)로 정했다.",
        "transcriptSeqs": [40, 42], "edited": False, "needsCheck": False,
        "sources": [{"agendaId": 101, "kind": "decision", "id": None}],
    }
    room = section(sections, "할 일")["evidence"][1]
    assert room["transcriptSeqs"] == [] and room["edited"] is True       # 사람이 점검에서 넣은 할 일


def test_빈_칸은_해당_내용_없음(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    empty = section(sections, "고객 요청")
    assert empty == {"title": "고객 요청", "content": EMPTY_SECTION, "evidence": []}


def test_content는_줄_목록과_정확히_같다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    for s in sections:
        assert s["content"] == content_of(s["evidence"])
        if s["evidence"]:
            assert s["content"].split("\n") == [f"- {line['sentence']}" for line in s["evidence"]]


def test_넣지_못한_재료를_출처로_돌려준다(materials):
    _, unplaced = build_sections(ITEMS, materials, placement(unplaced=("m9",)))
    assert unplaced == [{"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20"}]
    sections, _ = build_sections(ITEMS, materials, placement())
    placed = [line["sources"][0] for s in sections for line in s["evidence"]]
    assert {"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20"} not in placed


def test_결과는_JSON으로_바꿀_수_있다(materials):
    sections, unplaced = build_sections(ITEMS, materials, placement())
    json.dumps({"sections": sections, "unplaced": unplaced}, ensure_ascii=False)
