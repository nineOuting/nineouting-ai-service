"""손 시험 도구 출력 시험. 다시 배치에서 바꾸기 전 초안과 줄마다 이전 칸이 보이는지, --relayout-to 흐름이 도는지 본다."""
import json
import sys
from types import SimpleNamespace

import pytest

from minutes import try_draft
from minutes.draft import make_draft
from minutes_fakes import FIXTURE, GOOD, ScriptLLM, load_request, previous_lines_of

ARGS = SimpleNamespace(timeout=60.0, retries=2, show_io=False, json=False)


def test_다시_배치는_바꾸기_전_초안과_줄마다_이전_칸을_보여준다(capsys):
    body = load_request()
    lines = previous_lines_of(make_draft(body, ScriptLLM(GOOD)))       # 결정 줄: "…로 정했다."
    lines.append({"title": "할 일", "sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [],
                  "edited": False, "needsCheck": False, "sources": [], "draftEdited": True})
    second = dict(body, template={"items": ["결정", "기타"]}, previousLines=lines)
    answer = {"placements": [{"m": f"m{i}", "section": 2} for i in range(1, 11) if i != 4]
              + [{"m": "m4", "section": 1, "text": "1차 배포를 10월 30일(목)로 확정했다."}], "unplaced": []}
    try_draft.run(second, try_draft.Recorder(ScriptLLM(answer)), ARGS, lines, body["template"]["items"])
    out = capsys.readouterr().out
    assert "바꾸기 전 초안 (안건별 논의 / 결정 사항 / 미결 안건 / 할 일)" in out and "바꾼 뒤 (결정 / 기타)" in out
    assert "- 배포 일정: 1차 배포를 10월 30일(목)로 확정했다.   ← 이전: 결정 사항 (다시 다듬음)" in out
    assert "- [새 줄] 고객사 로고는 행사 전날까지 받기   ← 이전: 할 일" in out
    assert "넣지 못한 이전 줄 (이전 칸 → 글)\n없음" in out


def test_relayout_to는_처음_초안을_만들고_그_줄로_다시_배치한다(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["try_draft", "--relayout-to", "결정 사항, 할 일 ,고객 요청"])
    try_draft.main()
    out = capsys.readouterr().out
    assert "1. 처음 초안 (안건별 논의 / 결정 사항 / 미결 안건 / 할 일)" in out
    assert "바꾼 뒤 (결정 사항 / 할 일 / 고객 요청)" in out
    assert "← 이전: 안건별 논의" in out                              # 가짜 AI 는 모두 첫 칸에 넣는다


def test_relayout_to는_다시_배치_요청에는_쓰지_않는다(monkeypatch):
    path = str(FIXTURE.parent / "relayout_request.json")
    assert json.loads(open(path, encoding="utf-8").read())["previousLines"]
    monkeypatch.setattr(sys, "argv", ["try_draft", "--file", path, "--relayout-to", "가,나"])
    with pytest.raises(SystemExit, match="처음 만들기 요청"):
        try_draft.main()
