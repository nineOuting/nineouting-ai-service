"""요약 → 재료 목록 시험. 합성 요청(fixtures/draft_request.json)을 쓴다."""
import copy
import json
from pathlib import Path

import pytest

from minutes.materials import NO_ASSIGNEE, NO_DUE, TODO_UNCLEAR, build_materials, todo_text

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
        ("m8", 103, "decisionCheck"), ("m9", 103, "opinion"), ("m10", 103, "todo"),
    ]
    assert [m.order for m in ms] == sorted(m.order for m in ms)


def test_결정은_decisionText_그대로이고_근거와_표시는_그_사항의_값(request_body):
    m = by_kind(materials_of(request_body), "decision")[0]
    assert m.text == "1차 배포를 10월 30일(목)로 정함"
    assert m.source() == {"agendaId": 101, "kind": "decision", "id": None, "decisionPointId": 7}
    assert m.evidence_seqs == (40, 42)
    assert (m.edited, m.needs_check) == (False, False)


def test_안건의_statusReason과_status는_보지_않는다(request_body):
    agenda(request_body)["statusReason"] = {"text": "결정할 사항 1개가 모두 결정됨", "evidenceSeqs": [1]}
    agenda(request_body)["status"] = "UNDECIDED"
    m = by_kind(materials_of(request_body), "decision")[0]
    assert m.text == "1차 배포를 10월 30일(목)로 정함" and m.evidence_seqs == (40, 42)


def test_미결은_사항_문구를_쓰고_근거는_그_사항의_줄(request_body):
    m = by_kind(materials_of(request_body), "undecided")[0]
    assert m.text == "광고 예산 승인"
    assert m.item_id is None and m.source() == {"agendaId": 102, "kind": "undecided", "id": None, "decisionPointId": 9}
    assert m.evidence_seqs == (88,)


def test_결정_확인_필요는_사항_문구에_결정_후보를_붙인다(request_body):
    m = by_kind(materials_of(request_body), "decisionCheck")[0]
    assert m.text == "회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함)"
    assert m.evidence_seqs == (101, 102) and m.decision_point_id == 12


def test_짝이_맞는_사항_문구가_없으면_decisionText를_쓴다(request_body):
    for a in request_body["agendas"]:
        a["decisionPoints"][0]["decisionPointId"] += 100           # 요약의 번호와 짝이 맞지 않게
    ms = materials_of(request_body)
    assert by_kind(ms, "undecided")[0].text == "월 사용량을 확인하기 전이라 승인 여부를 정하지 못함"
    assert by_kind(ms, "decisionCheck")[0].text == "(결정 확인 필요: 강남역 근처로 정함)"


def test_결정할_사항마다_줄_하나이고_안건_안에서_사항_순서를_따른다(request_body):
    """안건은 미결이어도 그 안에 결정된 사항이 있을 수 있다. 줄은 사항마다 나오고 순서는 안건 목록의 position."""
    request_body["agendas"][1]["decisionPoints"] = [
        {"decisionPointId": 9, "position": 2, "text": "광고 예산 승인"},
        {"decisionPointId": 10, "position": 1, "text": "홍보 채널 확정"},
        {"decisionPointId": 11, "position": 3, "text": "집행 시기"},
        {"decisionPointId": 13, "position": 4, "text": "담당 부서"},
    ]
    agenda(request_body, 1)["decisionPoints"] = [
        {"decisionPointId": 9, "status": "UNDECIDED", "decisionText": "승인 여부를 정하지 못함", "evidenceSeqs": [88],
         "edited": False, "needsCheck": False},
        {"decisionPointId": 10, "status": "DECIDED", "decisionText": "홍보는 사내 메일로 함", "evidenceSeqs": [80],
         "edited": True, "needsCheck": True},
        {"decisionPointId": 11, "status": "NOT_STARTED", "decisionText": "아직 논의되지 않았어요", "evidenceSeqs": [],
         "edited": False, "needsCheck": False},
        {"decisionPointId": 13, "status": "DECISION_CHECK", "decisionText": "마케팅팀이 맡음", "evidenceSeqs": [90],
         "edited": False, "needsCheck": False},
    ]
    points = [m for m in materials_of(request_body) if m.agenda_id == 102 and m.decision_point_id is not None]
    assert [(m.kind, m.decision_point_id, m.text) for m in points] == [
        ("decision", 10, "홍보는 사내 메일로 함"),
        ("undecided", 9, "광고 예산 승인"),
        ("decisionCheck", 13, "담당 부서 (결정 확인 필요: 마케팅팀이 맡음)"),
    ]
    assert (points[0].edited, points[0].needs_check, points[0].evidence_seqs) == (True, True, (80,))


def test_안건_목록에_없는_사항은_그_안건의_사항_줄_뒤에(request_body):
    agenda(request_body, 1)["decisionPoints"].insert(0, {
        "decisionPointId": 99, "status": "DECIDED", "decisionText": "회의 중 생긴 사항을 정함", "evidenceSeqs": [],
        "edited": False, "needsCheck": False})
    points = [m for m in materials_of(request_body) if m.agenda_id == 102 and m.decision_point_id is not None]
    assert [m.decision_point_id for m in points] == [9, 99]


def test_전환_기간_decisionPoint_글은_번호_없는_사항_하나(request_body):
    for a in request_body["agendas"]:
        a["decisionPoint"] = a.pop("decisionPoints")[0]["text"]
    for a in request_body["summary"]["agendas"]:
        for p in a["decisionPoints"]:
            p["decisionPointId"] = None
    ms = materials_of(request_body)
    assert by_kind(ms, "undecided")[0].text == "광고 예산 승인"
    assert by_kind(ms, "decisionCheck")[0].text == "회식 장소 확정 (결정 확인 필요: 강남역 근처로 정함)"
    assert all(m.decision_point_id is None for m in ms)


def test_결정할_사항_문구가_없는_안건은_decisionText를_쓴다(request_body):
    """v0.7 까지 decisionPoint 는 선택이었다. 둘 다 없으면 사항 문구 없이 받는다 (명세 5.1)."""
    for a in request_body["agendas"]:
        del a["decisionPoints"]
    ms = materials_of(request_body)
    assert by_kind(ms, "undecided")[0].text == "월 사용량을 확인하기 전이라 승인 여부를 정하지 못함"
    assert by_kind(ms, "decisionCheck")[0].text == "(결정 확인 필요: 강남역 근처로 정함)"


def test_논의_전인_사항은_줄이_없다(request_body):
    assert not [m for m in materials_of(request_body) if m.agenda_id == 104]


def test_요약과_의견은_원래_글_그대로(request_body):
    ms = materials_of(request_body)
    summary_101, summary_102 = by_kind(ms, "summary")
    assert summary_101.text == "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴"
    assert summary_102.text == "광고 예산은 월 사용량을 확인한 뒤 정하기로 함"
    assert summary_102.edited
    opinion = by_kind(ms, "opinion")[0]
    assert opinion.text == "목요일은 QA 일정과 겹쳐 부담이 된다는 우려"
    assert opinion.label == "우려"


def test_할_일은_담당자와_기한을_붙이고_없으면_미정(request_body):
    todo_101, todo_102, todo_103 = by_kind(materials_of(request_body), "todo")
    assert todo_101.text == "QA 일정표 공유 (김철수, 수요일까지)"
    assert todo_102.text == f"회의실 예약 (이영희, {NO_DUE})"
    assert todo_103.text == f"식당 후보 알아보기 ({NO_ASSIGNEE}, {NO_DUE}, {TODO_UNCLEAR})"
    assert todo_text({"task": "체크리스트 작성", "assigneeName": None, "dueText": None}) == \
        f"체크리스트 작성 ({NO_ASSIGNEE}, {NO_DUE})"


def test_근거와_표시를_그대로_옮긴다(request_body):
    ms = {m.ref: m for m in materials_of(request_body)}
    assert ms["m7"].evidence_seqs == () and ms["m7"].edited          # 사람이 점검에서 넣은 할 일
    assert ms["m9"].needs_check and ms["m9"].evidence_seqs == (103,)
    assert ms["m3"].source() == {"agendaId": 101, "kind": "opinion", "id": "k3f9a1-7", "decisionPointId": None}


def test_요약이_비면_재료도_없다(request_body):
    body = copy.deepcopy(request_body)
    body["summary"]["agendas"] = []
    assert materials_of(body) == []


@pytest.mark.parametrize("text", [
    "클라우드 자격증 「세개」(3개로 읽힘)를 따겠다는 계획이 나옴",
    "「기라」(지라로 읽힘, STT 불확실)를 쓰자는 제안이 나옴",
    "STT 불확실 구간에서 일정 이야기가 나옴",
])
def test_STT_불확실_표시가_있는_글도_그대로(request_body, text):
    agenda(request_body)["summary"][0]["text"] = text
    agenda(request_body)["opinions"][0]["text"] = text
    agenda(request_body)["decisionPoints"][0]["decisionText"] = text
    ms = materials_of(request_body)
    for kind in ("summary", "opinion", "decision"):
        assert by_kind(ms, kind)[0].text == text


def agenda(body, i=0):
    return body["summary"]["agendas"][i]
