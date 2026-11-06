"""배치 결과 → sections 시험. 배치는 AI 대신 손으로 정해 넣는다."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from minutes.lines import EMPTY_SECTION, build_sections, content_of
from minutes.materials import build_materials
from minutes.models import TABLE, Field, Placement, Table

FIXTURE = Path(__file__).parent / "fixtures" / "draft_request.json"
ITEMS = ["안건별 논의", "결정 사항", "미결 안건", "할 일", "고객 요청"]

# 칸 위치: 0 안건별 논의, 1 결정 사항, 2 미결 안건, 3 할 일, 4 고객 요청 (빈 칸)
SECTIONS = {"m1": 1, "m2": 0, "m3": 0, "m4": 3, "m5": 2, "m6": 0, "m7": 3, "m8": 2, "m10": 3}


@pytest.fixture
def materials():
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return build_materials(body["agendas"], body["summary"])


def placement(sections=None, unplaced=("m9",), grouping=None):
    return Placement(sections=dict(sections or SECTIONS), unplaced=tuple(unplaced), grouping=dict(grouping or {}))


def section(sections, title):
    return next(s for s in sections if s["title"] == title)


def test_칸은_양식_순서와_이름_그대로(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [s["title"] for s in sections] == ITEMS


def test_줄_앞에_안건_제목을_붙인다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [line["sentence"] for line in section(sections, "할 일")["evidence"]] == [
        "배포 일정: QA 일정표 공유 (김철수, 수요일까지)",
        "예산: 회의실 예약 (이영희, 기한 미정)",
        "회식 장소: 식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)",
    ]
    assert section(sections, "결정 사항")["evidence"][0]["sentence"] == "배포 일정: 1차 배포를 10월 30일(목)로 정함"


def test_미결과_결정_확인_필요_줄(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    assert [line["sentence"] for line in section(sections, "미결 안건")["evidence"]] == [
        "예산: 월 사용량을 확인하기 전이라 승인 여부를 정하지 못함",
        "회식 장소: 회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함)",
    ]


def test_칸_안_순서는_AI_순서가_아니라_재료_순서(materials):
    shuffled = dict(reversed(list(SECTIONS.items())))
    sections, _ = build_sections(ITEMS, materials, placement(sections=shuffled))
    discussion = section(sections, "안건별 논의")["evidence"]
    assert [line["sources"][0]["id"] for line in discussion] == ["k3f9a1-1", "k3f9a1-7", "k3f9a1-3"]


def test_근거_표시_출처를_재료에서_옮긴다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    decision = section(sections, "결정 사항")["evidence"][0]
    assert decision == {
        "sentence": "배포 일정: 1차 배포를 10월 30일(목)로 정함",
        "transcriptSeqs": [40, 42], "edited": False, "needsCheck": False,
        "sources": [{"agendaId": 101, "kind": "decision", "id": None, "decisionPointId": 7}],
    }
    room = section(sections, "할 일")["evidence"][1]
    assert room["transcriptSeqs"] == [] and room["edited"] is True       # 사람이 점검에서 넣은 할 일


def test_빈_칸은_해당_내용_없음(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    empty = section(sections, "고객 요청")
    assert empty == {"title": "고객 요청", "content": EMPTY_SECTION, "evidence": []}


def test_묶지_않으면_content는_줄_목록과_정확히_같다(materials):
    sections, _ = build_sections(ITEMS, materials, placement())
    for s in sections:
        if s["evidence"]:
            assert s["content"].split("\n") == [f"- {line['sentence']}" for line in s["evidence"]]


def test_안건으로_묶으면_안건_제목_아래에_제목을_뗀_글(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={0: "안건"}))
    assert section(sections, "안건별 논의")["content"] == "\n".join([
        "- 배포 일정",
        "  - 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴",
        "  - 목요일은 QA 일정과 겹쳐 부담이 된다는 우려",
        "- 예산",
        "  - 광고 예산은 월 사용량을 확인한 뒤 정하기로 함",
    ])


def test_담당자로_묶으면_담당자_없는_할_일은_묶음_뒤(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={3: "담당자"}))
    assert section(sections, "할 일")["content"] == "\n".join([
        "- 김철수",
        "  - 배포 일정: QA 일정표 공유 (김철수, 수요일까지)",
        "- 이영희",
        "  - 예산: 회의실 예약 (이영희, 기한 미정)",
        "- 회식 장소: 식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)",
    ])


def test_종류로_묶으면_처음_나온_순서로_묶음을_둔다(materials):
    everything = {m.ref: 0 for m in materials}
    sections, _ = build_sections(ITEMS, materials, placement(sections=everything, unplaced=(), grouping={0: "종류"}))
    content = section(sections, "안건별 논의")["content"].split("\n")
    assert [line for line in content if not line.startswith(" ")] == [
        "- 결정", "- 요약", "- 의견", "- 할 일", "- 미결", "- 결정 확인 필요"]
    assert content[content.index("- 의견") + 1:content.index("- 할 일")] == [
        "  - 배포 일정: 목요일은 QA 일정과 겹쳐 부담이 된다는 우려",
        "  - 회식 장소: 회식 예산이 1인당 3만 원을 넘으면 안 된다는 의견",
    ]


def test_기준_값이_하나뿐이면_묶지_않는다(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={1: "안건", 2: "종류"}))
    assert section(sections, "결정 사항")["content"] == "- 배포 일정: 1차 배포를 10월 30일(목)로 정함"     # 안건 1개
    one_kind = [m for m in materials if m.kind == "summary"]
    assert content_of(one_kind, "종류") == content_of(one_kind)                                          # 종류 1개


def test_담당자가_1명_이하이면_안건으로_묶는다(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={2: "담당자"}))
    assert section(sections, "미결 안건")["content"] == "\n".join([                                    # 담당자 0명
        "- 예산", "  - 월 사용량을 확인하기 전이라 승인 여부를 정하지 못함",
        "- 회식 장소", "  - 회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함)"])
    todos = [replace(m, parts=(*[p for p in m.parts if p[0] != "담당자"], ("담당자", "김철수")))
             for m in materials if m.kind == "todo"]
    assert len({m.agenda_id for m in todos}) > 1
    assert content_of(todos, "담당자") == content_of(todos, "안건")                                       # 담당자 1명


def test_담당자가_2명_이상이면_담당자로_묶는다(materials):
    todos = [m for m in materials if m.kind == "todo"]
    assert len({dict(m.parts).get("담당자") for m in todos} - {None}) >= 2
    text = content_of(todos, "담당자")
    assert text != content_of(todos, "안건") and "- 김철수\n  - " in text


def test_안건도_하나뿐이면_담당자_기준은_묶지_않는다(materials):
    one = [m for m in materials if m.agenda_id == 101 and m.kind == "todo"]
    assert content_of(one, "담당자") == content_of(one)


def test_묶어도_줄_목록은_그대로(materials):
    flat, _ = build_sections(ITEMS, materials, placement())
    grouped, _ = build_sections(ITEMS, materials, placement(grouping={0: "안건", 3: "담당자"}))
    assert [s["evidence"] for s in grouped] == [s["evidence"] for s in flat]
    assert section(grouped, "고객 요청")["content"] == EMPTY_SECTION


def test_표_필드는_묶는_기준을_쓰지_않는다(materials):
    fields = [*ITEMS[:3], Field("할 일", type=TABLE), ITEMS[4]]
    table = Table(("할 일",), {"m4": ("QA 일정표 공유",), "m7": ("회의실 예약",), "m10": ("식당 후보",)})
    plain, _ = build_sections(fields, materials, placement(), tables={3: table})
    grouped, _ = build_sections(fields, materials, placement(grouping={3: "담당자"}), tables={3: table})
    assert section(grouped, "할 일") == section(plain, "할 일")
    assert section(grouped, "할 일")["content"].startswith("| 할 일 |")


def test_이전_줄_글은_안건_제목이_앞에_있을_때만_뗀다(materials):
    m2, m3, m6 = (next(m for m in materials if m.ref == r) for r in ("m2", "m3", "m6"))
    kept = [replace(m2, text="배포 일정: 작성자가 고친 글", whole_line=True),
            replace(m3, text="제목을 지운 글", whole_line=True),
            m6,
            replace(m6, ref="m11", agenda_id=None, agenda_title="", text="작성자가 새로 쓴 줄", whole_line=True)]
    assert content_of(kept, "안건") == "\n".join([
        "- 배포 일정", "  - 작성자가 고친 글", "  - 제목을 지운 글",
        "- 예산", "  - 광고 예산은 월 사용량을 확인한 뒤 정하기로 함",
        "- 작성자가 새로 쓴 줄",
    ])


def test_넣지_못한_재료를_출처로_돌려준다(materials):
    _, unplaced = build_sections(ITEMS, materials, placement(unplaced=("m9",)))
    assert unplaced == [{"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20", "decisionPointId": None}]
    sections, _ = build_sections(ITEMS, materials, placement())
    placed = [line["sources"][0] for s in sections for line in s["evidence"]]
    assert {"agendaId": 103, "kind": "opinion", "id": "k3f9a1-20", "decisionPointId": None} not in placed


def test_결과는_JSON으로_바꿀_수_있다(materials):
    sections, unplaced = build_sections(ITEMS, materials, placement())
    json.dumps({"sections": sections, "unplaced": unplaced}, ensure_ascii=False)
