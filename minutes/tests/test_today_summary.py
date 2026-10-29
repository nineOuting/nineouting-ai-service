"""회의 끝 요약(AI-08) 공식 예시와의 호환 시험.

examples/api/agenda-board-today-summary-put.json 의 보드 todaySummary 칸을 그대로 초안 요청의 summary 로,
examples/api/agendas-decision-points-put.json 의 안건 목록을 agendas 로 넣는다.
예시가 바뀌면(요약 담당의 형식 변경) 이 시험이 먼저 알려 준다.
"""
import copy
import json
from pathlib import Path

import pytest

from minutes.draft import SUCCEEDED, make_draft
from minutes.materials import build_materials
from minutes.request import validate_request
from minutes.try_draft import PlaceAllFakeLLM

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "api"
BOARD = EXAMPLES / "agenda-board-today-summary-put.json"
AGENDAS = EXAMPLES / "agendas-decision-points-put.json"


@pytest.fixture
def body():
    today = json.loads(BOARD.read_text(encoding="utf-8"))["todaySummary"]
    agendas = json.loads(AGENDAS.read_text(encoding="utf-8"))["agendas"]           # 백엔드 안건 목록 그대로
    return {"requestId": "today-summary-example", "minutesId": 1, "meetingId": 1,
            "template": {"items": ["안건별 논의", "결정 사항", "미결 안건", "할 일"]},
            "agendas": agendas, "summary": today, "previousLines": [], "callbackUrl": "http://127.0.0.1/unused"}


def test_todaySummary_칸을_그대로_넣어도_요청_검사를_통과한다(body):
    assert {"requestRev", "final"} <= set(body["summary"])         # 보드의 칸이 붙은 그대로
    validate_request(body)


def test_백엔드가_칸을_빼고_meetingId를_붙여도_통과한다(body):
    summary = copy.deepcopy(body["summary"])
    del summary["requestRev"], summary["final"]
    summary["meetingId"] = 1
    validate_request(dict(body, summary=summary))


def test_모든_항목이_줄로_한_번씩_나온다(body):
    materials = build_materials(body["agendas"], body["summary"])
    items = sum(len(a["summary"]) + len(a["opinions"]) + len(a["todos"]) for a in body["summary"]["agendas"])
    point_lines = sum(1 for a in body["summary"]["agendas"] for p in a["decisionPoints"]
                      if p["status"] in ("DECIDED", "UNDECIDED", "DECISION_CHECK"))
    assert point_lines > 0 and len(materials) == items + point_lines
    result = make_draft(body, PlaceAllFakeLLM())
    assert result["status"] == SUCCEEDED
    assert sum(len(s["evidence"]) for s in result["sections"]) == len(materials)


def test_결정과_미결_줄은_사항마다_나오고_개수_문장을_쓰지_않는다(body):
    result = make_draft(body, PlaceAllFakeLLM())
    lines = [line for s in result["sections"] for line in s["evidence"]]
    sentences = [line["sentence"] for line in lines]
    assert "배포 일정: 1차 배포는 다음 주 목요일로 정한다" in sentences
    assert "예산 배분: 외부 API 비용 한도 확정" in sentences                   # 미결은 사항 문구
    assert not any("결정할 사항" in s or "아직 논의되지 않았어요" in s for s in sentences)
    points = {line["sources"][0]["decisionPointId"] for line in lines if line["sources"][0]["decisionPointId"]}
    assert points == {7, 10}                                                  # 논의 전 사항(11, 12)은 줄이 없다


def test_근거와_표시를_예시에서_그대로_옮긴다(body):
    result = make_draft(body, PlaceAllFakeLLM())
    lines = {(line["sources"][0]["kind"], line["sources"][0]["id"]): line
             for s in result["sections"] for line in s["evidence"]}
    for agenda in body["summary"]["agendas"]:
        for kind, key in (("summary", "summary"), ("opinion", "opinions"), ("todo", "todos")):
            for item in agenda[key]:
                line = lines[(kind, item["id"])]
                assert line["transcriptSeqs"] == item["evidenceSeqs"]
                assert (line["edited"], line["needsCheck"]) == (item["edited"], item["needsCheck"])
