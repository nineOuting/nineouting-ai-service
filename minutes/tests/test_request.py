"""요청 검사 시험. 합성 요청을 한 곳씩 망가뜨려 맞는 오류가 나는지 본다."""
import pytest

from minutes.request import InvalidInput, validate_request
from minutes_fakes import load_request


def broken(change):
    body = load_request()
    change(body)
    return body


def agenda(body, i=0):
    return body["summary"]["agendas"][i]


# 다시 배치의 이전 줄 하나 (명세 5.7). 출처 id 는 형식만 맞으면 되고 요약에 없어도 된다
LINE = {"title": "할 일", "sentence": "배포 일정: QA 일정표 공유 (김철수, 수요일까지)", "transcriptSeqs": [43],
        "edited": False, "needsCheck": False, "sources": [{"agendaId": 101, "kind": "todo", "id": "x-1"}],
        "draftEdited": False}


def test_합성_요청은_통과한다():
    validate_request(load_request())


def test_쓰지_않는_칸과_보드의_다른_칸은_있어도_된다():
    body = load_request()
    body["transcripts"] = [{"seq": 1, "text": "x"}]
    body["summary"].update(version=7, basedOnSeq=120, currentAgendaId=None, offTopic=None, leftover={"final": True})
    validate_request(body)


def test_이전_줄은_명세_8_3의_경우를_거절하지_않는다():
    """새로 쓴 줄(출처 없음), 출처가 요약에 없는 줄, 출처가 겹치는 줄, 표시 칸이 빠진 줄, 빈 목록"""
    new = dict(LINE, sources=[], transcriptSeqs=[], draftEdited=True)
    bare = {k: v for k, v in LINE.items() if k not in ("edited", "needsCheck")}
    for lines in ([new], [LINE], [LINE, dict(LINE, draftEdited=True)], [bare], []):
        body = load_request()
        body["previousLines"] = lines
        validate_request(body)


def test_안건의_statusReason과_모르는_사항_상태는_보지_않는다():
    body = load_request()
    agenda(body)["statusReason"] = "결정할 사항 1개가 모두 결정됨"
    agenda(body, 1).pop("statusReason")
    agenda(body)["decisionPoints"][0]["status"] = "DISCUSSING"
    validate_request(body)


def test_결정할_사항_전환_기간_모양도_받는다():
    """decisionPoints 대신 decisionPoint 글 하나, 둘 다 없음, 요약 쪽 사항 번호 null (명세 5.1·5.2)"""
    body = load_request()
    body["agendas"][0]["decisionPoint"] = body["agendas"][0].pop("decisionPoints")[0]["text"]
    del body["agendas"][1]["decisionPoints"]
    agenda(body)["decisionPoints"][0]["decisionPointId"] = None
    agenda(body, 1)["decisionPoints"][0]["decisionPointId"] = None
    body["agendas"][2]["decisionPoints"][0]["decisionPointId"] = "dp-12"           # 문자열 번호
    agenda(body, 2)["decisionPoints"][0]["decisionPointId"] = "dp-12"
    validate_request(body)


def test_빠진_목록과_표시는_너그럽게_본다():
    body = load_request()
    del agenda(body)["opinions"]
    del agenda(body)["summary"][0]["edited"]
    agenda(body)["opinions"] = None
    validate_request(body)


@pytest.mark.parametrize("change, expected", [
    (lambda b: b.clear() or b.update(x=1), "requestId"),
    (lambda b: b.update(requestId=""), "requestId가 비었거나"),
    (lambda b: b.update(requestId="r" * 201), "requestId는 200자"),
    (lambda b: b.update(minutesId="41"), "minutesId는 양의 정수"),
    (lambda b: b.update(meetingId=True), "meetingId는 양의 정수"),
    (lambda b: b.pop("callbackUrl"), "callbackUrl"),
    (lambda b: b.update(template={"items": []}), "template.items는 1~20개"),
    (lambda b: b.update(template={"items": [f"칸{i}" for i in range(21)]}), "template.items는 1~20개"),
    (lambda b: b.update(template={"items": ["할 일", " "]}), "template.items[1]가 비었거나"),
    (lambda b: b.update(template={"items": ["가" * 101]}), "template.items[0]는 100자"),
    (lambda b: b.update(template={"items": ["할 일", "할 일 "]}), "template.items[1]의 칸 이름이 앞의 칸과 겹침"),
    (lambda b: b["agendas"].append(dict(b["agendas"][0])), "agendas[4].agendaId가 앞의 안건과 겹침"),
    (lambda b: b["agendas"][0].update(title=None), "agendas[0].title"),
    (lambda b: b["agendas"][0].update(decisionPoint=3), "agendas[0].decisionPoint는 문자열이나 null"),
    (lambda b: b.update(summary={"agendas": None}), "summary.agendas가 목록이 아님"),
    (lambda b: agenda(b).update(agendaId=999), "summary.agendas[0].agendaId가 agendas에 없음"),
    (lambda b: agenda(b, 1).update(agendaId=101), "summary.agendas[1].agendaId가 앞의 안건과 겹침"),
    (lambda b: agenda(b).update(status="DONE"), "summary.agendas[0].status는"),
    (lambda b: b["agendas"][0].update(decisionPoints=[]), "agendas[0].decisionPoints는 1개 이상의 목록"),
    (lambda b: b["agendas"][0].update(decisionPoints="배포 날짜"), "agendas[0].decisionPoints는 1개 이상의 목록"),
    (lambda b: b["agendas"][0]["decisionPoints"][0].update(decisionPointId=True),
     "agendas[0].decisionPoints[0].decisionPointId는 양의 정수나 문자열"),
    (lambda b: b["agendas"][0]["decisionPoints"][0].update(decisionPointId=None),
     "agendas[0].decisionPoints[0].decisionPointId는 양의 정수나 문자열"),
    (lambda b: b["agendas"][1]["decisionPoints"][0].update(decisionPointId=7),
     "agendas[1].decisionPoints[0].decisionPointId가 앞의 사항과 겹침"),
    (lambda b: b["agendas"][0]["decisionPoints"][0].update(position="1"), "agendas[0].decisionPoints[0].position이 정수"),
    (lambda b: b["agendas"][0]["decisionPoints"][0].update(text=" "), "agendas[0].decisionPoints[0].text가 비었거나"),
    (lambda b: agenda(b).pop("decisionPoints"), "summary.agendas[0].decisionPoints가 목록이 아님"),
    (lambda b: agenda(b)["decisionPoints"][0].update(decisionText=""),
     "summary.agendas[0].decisionPoints[0].decisionText가 비었거나"),
    (lambda b: agenda(b)["decisionPoints"][0].update(status=None), "summary.agendas[0].decisionPoints[0].status"),
    (lambda b: agenda(b)["decisionPoints"][0].update(evidenceSeqs=None),
     "summary.agendas[0].decisionPoints[0].evidenceSeqs는 0 이상 정수"),
    (lambda b: agenda(b)["decisionPoints"][0].update(edited="no"),
     "summary.agendas[0].decisionPoints[0].edited가 true/false가 아님"),
    (lambda b: agenda(b, 1)["decisionPoints"][0].update(decisionPointId=7),
     "summary.agendas[1].decisionPoints[0].decisionPointId가 앞의 사항과 겹침"),
    (lambda b: agenda(b).update(todos="없음"), "summary.agendas[0].todos가 목록이 아님"),
    (lambda b: agenda(b)["todos"][0].update(task=""), "summary.agendas[0].todos[0].task가 비었거나"),
    (lambda b: agenda(b)["summary"][0].update(id=None), "summary.agendas[0].summary[0].id"),
    (lambda b: agenda(b, 1)["summary"][0].update(id="k3f9a1-1"), "summary.agendas[1].summary[0].id가 앞의 항목과 겹침"),
    (lambda b: agenda(b)["opinions"][0].update(evidenceSeqs=None), "opinions[0].evidenceSeqs는 0 이상 정수"),
    (lambda b: agenda(b)["opinions"][0].update(evidenceSeqs=[-1]), "opinions[0].evidenceSeqs는 0 이상 정수"),
    (lambda b: agenda(b)["summary"][0].update(edited="yes"), "summary[0].edited가 true/false가 아님"),
    (lambda b: agenda(b)["todos"][0].update(dueText=3), "todos[0].dueText는 문자열이나 null"),
    (lambda b: b.update(previousLines={"title": "할 일"}), "previousLines는 600개 이하의 목록"),
    (lambda b: b.update(previousLines=[LINE] * 601), "previousLines는 600개 이하의 목록"),
    (lambda b: b.update(previousLines=[{"title": "할 일"}]), "previousLines[0].sentence가 비었거나"),
    (lambda b: b.update(previousLines=[dict(LINE, title="")]), "previousLines[0].title가 비었거나"),
    (lambda b: b.update(previousLines=[dict(LINE, sentence="가" * 2001)]), "previousLines[0].sentence는 2000자"),
    (lambda b: b.update(previousLines=[dict(LINE, transcriptSeqs=None)]), "previousLines[0].transcriptSeqs는 0 이상 정수"),
    (lambda b: b.update(previousLines=[{k: v for k, v in LINE.items() if k != "draftEdited"}]),
     "previousLines[0].draftEdited가 true/false가 아님"),
    (lambda b: b.update(previousLines=[dict(LINE, edited="no")]), "previousLines[0].edited가 true/false가 아님"),
    (lambda b: b.update(previousLines=[dict(LINE, sources=None)]), "previousLines[0].sources가 목록이 아님"),
    (lambda b: b.update(previousLines=[dict(LINE, sources=[{"agendaId": "101", "kind": "todo", "id": "x"}])]),
     "previousLines[0].sources[0].agendaId는 양의 정수"),
    (lambda b: b.update(previousLines=[dict(LINE, sources=[{"agendaId": 101, "kind": "memo", "id": "x"}])]),
     "previousLines[0].sources[0].kind는"),
    (lambda b: b.update(previousLines=[dict(LINE, sources=[{"agendaId": 101, "kind": "todo", "id": 3}])]),
     "previousLines[0].sources[0].id는 문자열이나 null"),
    (lambda b: b.update(previousLines=[dict(LINE, sources=[{"agendaId": 101, "kind": "decision", "id": None,
                                                            "decisionPointId": 0}])]),
     "previousLines[0].sources[0].decisionPointId는 양의 정수나 문자열"),
])
def test_틀린_요청은_칸_위치를_담은_오류(change, expected):
    with pytest.raises(InvalidInput) as e:
        validate_request(broken(change))
    assert expected in str(e.value)


def test_오류_글에_회의_내용이_섞이지_않는다():
    body = broken(lambda b: agenda(b, 1)["summary"][0].update(id="k3f9a1-1"))
    with pytest.raises(InvalidInput) as e:
        validate_request(body)
    for word in ["k3f9a1-1", "광고", "예산"]:
        assert word not in str(e.value)


def test_본문이_객체가_아니면_오류():
    with pytest.raises(InvalidInput, match="JSON 객체가 아님"):
        validate_request([1, 2])
