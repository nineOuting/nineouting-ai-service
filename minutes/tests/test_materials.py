"""요약 → 재료 목록 시험. 합성 요청(fixtures/draft_request.json)을 쓴다."""
import copy
import json
from pathlib import Path

import pytest

from minutes.materials import NO_ASSIGNEE, NO_DUE, build_materials, decision_text, todo_text

FIXTURE = Path(__file__).parent / "fixtures" / "draft_request.json"


@pytest.fixture
def request_body():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def materials_of(body):
    return build_materials(body["agendas"], body["summary"])


def by_kind(materials, kind):
    return [m for m in materials if m.kind == kind]


def test_안건_순서와_종류_순서로_번호를_붙인다(request_body):
    ms = materials_of(request_body)
    assert [(m.ref, m.agenda_id, m.kind) for m in ms] == [
        ("m1", 101, "decision"), ("m2", 101, "summary"), ("m3", 101, "opinion"), ("m4", 101, "todo"),
        ("m5", 102, "undecided"), ("m6", 102, "summary"), ("m7", 102, "todo"),
        ("m8", 103, "decisionCheck"), ("m9", 103, "opinion"),
    ]
    assert [m.order for m in ms] == sorted(m.order for m in ms)


def test_결정은_결정과_맥락을_코드가_잇는다(request_body):
    m = by_kind(materials_of(request_body), "decision")[0]
    assert m.fixed_text == "10월 30일(목) 배포 (QA가 수요일까지 가능하다고 확인)"
    assert not m.rewrite
    assert m.source() == {"agendaId": 101, "kind": "decision", "id": "d-101"}
    assert m.evidence_seqs == (40, 42)
    assert decision_text({"text": "목요일 배포", "context": None}) == "목요일 배포"


def test_미결은_결정할_사항을_쓰고_근거는_판정_이유의_줄(request_body):
    m = by_kind(materials_of(request_body), "undecided")[0]
    assert m.fixed_text == "광고 예산 승인"
    assert m.item_id is None and m.source()["id"] is None
    assert m.evidence_seqs == (88,)
    assert (m.edited, m.needs_check) == (False, False)


def test_결정_확인_필요는_판정_이유를_붙이고_결정할_사항이_없으면_안건_제목(request_body):
    m = by_kind(materials_of(request_body), "decisionCheck")[0]
    assert m.fixed_text == "회식 장소 (결정 확인 필요: 강남역 근처로 하자는 말은 있으나 합의 표현이 없음)"
    assert m.evidence_seqs == (101, 102)


def test_아직_안_다룬_안건과_결정_없는_DECIDED는_상태_줄이_없다(request_body):
    assert not [m for m in materials_of(request_body) if m.agenda_id == 104]
    request_body["summary"]["agendas"][0]["decision"] = None
    assert not by_kind(materials_of(request_body), "decision")


def test_요약과_의견은_AI가_다듬고_사람이_고친_것은_그대로(request_body):
    ms = materials_of(request_body)
    summary_101, summary_102 = by_kind(ms, "summary")
    assert summary_101.rewrite and summary_101.text == "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴"
    assert not summary_102.rewrite and summary_102.fixed_text == "광고 예산은 월 사용량을 확인한 뒤 정하기로 함"
    assert summary_102.edited
    opinion = by_kind(ms, "opinion")[0]
    assert opinion.rewrite and opinion.label == "우려"


def test_할_일은_담당자와_기한을_붙이고_없으면_미정(request_body):
    todo_101, todo_102 = by_kind(materials_of(request_body), "todo")
    assert todo_101.fixed_text == "QA 일정표 공유 (김철수, 수요일까지)"
    assert todo_102.fixed_text == f"회의실 예약 (이영희, {NO_DUE})"
    assert todo_text({"task": "체크리스트 작성", "assigneeName": None, "dueText": None}) == \
        f"체크리스트 작성 ({NO_ASSIGNEE}, {NO_DUE})"


def test_근거와_표시를_그대로_옮긴다(request_body):
    ms = {m.ref: m for m in materials_of(request_body)}
    assert ms["m7"].evidence_seqs == () and ms["m7"].edited          # 사람이 점검에서 넣은 할 일
    assert ms["m9"].needs_check and ms["m9"].evidence_seqs == (103,)
    assert ms["m3"].source() == {"agendaId": 101, "kind": "opinion", "id": "k3f9a1-7"}


def test_요약이_비면_재료도_없다(request_body):
    body = copy.deepcopy(request_body)
    body["summary"]["agendas"] = []
    assert materials_of(body) == []
