"""배치 결과 → sections 시험. 배치는 AI 대신 손으로 정해 넣는다."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from minutes.lines import EMPTY_SECTION, build_sections, content_of, line_text
from minutes.materials import build_materials
from minutes.models import CHECKLIST, FREE, LIST, TABLE, TEXT, Field, Placement, Table

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
        "1. 배포 일정",
        "   - 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴",
        "   - 목요일은 QA 일정과 겹쳐 부담이 된다는 우려",
        "2. 예산",
        "   - 광고 예산은 월 사용량을 확인한 뒤 정하기로 함",
    ])


def test_담당자로_묶으면_담당자_없는_할_일은_묶음_뒤(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={3: "담당자"}))
    assert section(sections, "할 일")["content"] == "\n".join([
        "1. 김철수",
        "   - 배포 일정: QA 일정표 공유 (김철수, 수요일까지)",
        "2. 이영희",
        "   - 예산: 회의실 예약 (이영희, 기한 미정)",
        "- 회식 장소: 식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)",
    ])


def test_종류로_묶으면_처음_나온_순서로_묶음을_둔다(materials):
    everything = {m.ref: 0 for m in materials}
    sections, _ = build_sections(ITEMS, materials, placement(sections=everything, unplaced=(), grouping={0: "종류"}))
    content = section(sections, "안건별 논의")["content"].split("\n")
    assert [line for line in content if not line.startswith(" ")] == [
        "1. 결정", "2. 요약", "3. 의견", "4. 할 일", "5. 미결", "6. 결정 확인 필요"]
    assert content[content.index("3. 의견") + 1:content.index("4. 할 일")] == [
        "   - 배포 일정: 목요일은 QA 일정과 겹쳐 부담이 된다는 우려",
        "   - 회식 장소: 회식 예산이 1인당 3만 원을 넘으면 안 된다는 의견",
    ]


def test_기준_값이_하나뿐이면_묶지_않는다(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={1: "안건", 2: "종류"}))
    assert section(sections, "결정 사항")["content"] == "- 배포 일정: 1차 배포를 10월 30일(목)로 정함"     # 안건 1개
    one_kind = [m for m in materials if m.kind == "summary"]
    assert content_of(one_kind, "종류") == content_of(one_kind)                                          # 종류 1개


@pytest.mark.parametrize("by", ["안건", "담당자", "종류", "묶지 않음"])
def test_줄이_2개_이상이고_모두_한_안건이면_기준과_상관없이_안건_제목_아래로_묶는다(materials, by):
    one = [replace(m, kind="summary") for m in materials if m.agenda_id == 101 and m.kind in ("summary", "opinion")]
    assert len(one) == 2                                  # 종류 기준도 값이 하나가 되게 맞춘다
    assert content_of(one, by) == "\n".join([
        "1. 배포 일정",
        "   - 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴",
        "   - 목요일은 QA 일정과 겹쳐 부담이 된다는 우려"])


def test_한_안건이어도_작성자가_새로_쓴_줄이_섞이면_묶지_않음은_그대로다(materials):
    one = [m for m in materials if m.agenda_id == 101 and m.kind in ("summary", "opinion")]
    written = replace(one[0], agenda_id=None, agenda_title="", whole_line=True, text="새로 쓴 줄", sourced=False)
    assert content_of([*one, written]).split("\n") == [f"- {line_text(m)}" for m in [*one, written]]


def test_담당자가_1명_이하이면_안건으로_묶는다(materials):
    sections, _ = build_sections(ITEMS, materials, placement(grouping={2: "담당자"}))
    assert section(sections, "미결 안건")["content"] == "\n".join([                                    # 담당자 0명
        "1. 예산", "   - 월 사용량을 확인하기 전이라 승인 여부를 정하지 못함",
        "2. 회식 장소", "   - 회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함)"])
    todos = [replace(m, parts=(*[p for p in m.parts if p[0] != "담당자"], ("담당자", "김철수")))
             for m in materials if m.kind == "todo"]
    assert len({m.agenda_id for m in todos}) > 1
    assert content_of(todos, "담당자") == content_of(todos, "안건")                                       # 담당자 1명


def test_담당자가_2명_이상이면_담당자로_묶는다(materials):
    todos = [m for m in materials if m.kind == "todo"]
    assert len({dict(m.parts).get("담당자") for m in todos} - {None}) >= 2
    text = content_of(todos, "담당자")
    assert text != content_of(todos, "안건") and "1. 김철수\n   - " in text


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
        "1. 배포 일정", "   - 작성자가 고친 글", "   - 제목을 지운 글",
        "2. 예산", "   - 광고 예산은 월 사용량을 확인한 뒤 정하기로 함",
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


# 기록 방식별 모양 (명세 v0.9 7.2)

def test_체크리스트형은_줄_앞에_체크박스(materials):
    todos = [m for m in materials if m.kind == "todo"]
    assert content_of(todos, "담당자", CHECKLIST) == "\n".join([
        "1. 김철수",
        "   - [ ] 배포 일정: QA 일정표 공유 (김철수, 수요일까지)",
        "2. 이영희",
        "   - [ ] 예산: 회의실 예약 (이영희, 기한 미정)",
        "- [ ] 회식 장소: 식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)",
    ])
    assert content_of(todos, "묶지 않음", CHECKLIST).split("\n") == [f"- [ ] {line_text(m)}" for m in todos]


def test_자유_입력형은_굵은_제목과_문단(materials):
    discussion = [m for m in materials if m.ref in ("m2", "m3", "m6")]
    assert content_of(discussion, "안건", FREE) == (
        "**배포 일정**\n"
        "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴. 목요일은 QA 일정과 겹쳐 부담이 된다는 우려.\n"
        "\n"
        "**예산**\n"
        "광고 예산은 월 사용량을 확인한 뒤 정하기로 함.")


def test_자유_입력형을_묶지_않으면_문단_하나이고_마침표가_있으면_더_붙이지_않는다(materials):
    m2, m3 = (next(m for m in materials if m.ref == r) for r in ("m2", "m3"))
    ended = replace(m3, text="정말 겹칠까?")
    assert content_of([m2, ended], "묶지 않음", FREE) == (                                      # 한 안건이면 묶는다
        "**배포 일정**\n1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴. 정말 겹칠까?")
    other = replace(ended, agenda_id=None, agenda_title="", whole_line=True, text="새로 쓴 줄입니다.")
    assert content_of([m2, other], "묶지 않음", FREE) == (
        "배포 일정: 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴. 새로 쓴 줄입니다.")
    assert content_of([m2], "안건", FREE) == "배포 일정: 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴."


def test_자유_입력형의_제목_없는_줄은_묶음_뒤_문단(materials):
    m2, m3, m6 = (next(m for m in materials if m.ref == r) for r in ("m2", "m3", "m6"))
    written = replace(m6, ref="m11", agenda_id=None, agenda_title="", text="새로 쓴 줄", whole_line=True)
    assert content_of([m2, m3, m6, written], "안건", FREE).split("\n\n")[-1] == "새로 쓴 줄."


def test_text는_목록형과_같다(materials):
    for by in ("안건", "담당자", "묶지 않음"):
        assert content_of(materials, by, TEXT) == content_of(materials, by, LIST) == content_of(materials, by)


def test_묶음이_10개_이상이면_들여쓰기가_번호_폭을_따른다(materials):
    base = next(m for m in materials if m.ref == "m2")
    many = [replace(base, ref=f"x{i}", agenda_id=200 + i, agenda_title=f"안건 {i}", text=f"글 {i}") for i in range(1, 11)]
    lines = content_of(many, "안건").split("\n")
    assert lines[:2] == ["1. 안건 1", "   - 글 1"]
    assert lines[-2:] == ["10. 안건 10", "    - 글 10"]


def test_결과의_type은_받은_값_그대로(materials):
    fields = [Field("안건별 논의", type=FREE), Field("결정 사항", type=TEXT), Field("미결 안건", type=CHECKLIST),
              Field("할 일", type=LIST), Field("고객 요청", type=LIST)]
    sections, _ = build_sections(fields, materials, placement(grouping={0: "안건"}), v07=True)
    assert [s["type"] for s in sections] == ["free", "text", "checklist", "list", "list"]
    assert section(sections, "안건별 논의")["content"].startswith("**배포 일정**\n")
    assert section(sections, "미결 안건")["content"].startswith("- [ ] ")
