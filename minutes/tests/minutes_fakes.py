"""회의록 초안 시험의 공용 도구: 합성 요청, 정해 둔 답을 내는 가짜 AI, 맞는 답 예시.
rag/tests/_helpers.py 와 이름이 겹치지 않게 따로 이름을 붙였다 (두 폴더를 한 번에 돌릴 때)."""
import json
from pathlib import Path

from rag.llm import Reply

FIXTURE = Path(__file__).parent / "fixtures" / "draft_request.json"
ITEMS = ["안건별 논의", "결정 사항", "미결 안건", "할 일"]

# 합성 요청에 대한 맞는 답. 다듬기 재료는 m1(결정)뿐이고 나머지는 고정이라 text 를 쓰지 않는다
GOOD = {
    "placements": [
        {"m": "m1", "section": 2, "text": "1차 배포를 10월 30일(목)로 정했다."},
        {"m": "m2", "section": 1}, {"m": "m3", "section": 1},
        {"m": "m4", "section": 4}, {"m": "m5", "section": 3}, {"m": "m6", "section": 1},
        {"m": "m7", "section": 4}, {"m": "m8", "section": 3}, {"m": "m10", "section": 4},
    ],
    "unplaced": ["m9"],
}


def load_request() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ScriptLLM:
    """정해 둔 답(dict·글 또는 예외)을 차례로 낸다. 받은 입력을 기록한다."""
    name = "script"
    model = "script"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def generate(self, system, user, timeout_s):
        self.calls.append((system, user, timeout_s))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Reply(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False), 100, 20)
