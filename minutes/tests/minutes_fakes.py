"""회의록 초안 시험의 공용 도구: 합성 요청, 정해 둔 답을 내는 가짜 AI, 맞는 답 예시.
rag/tests/_helpers.py 와 이름이 겹치지 않게 따로 이름을 붙였다 (두 폴더를 한 번에 돌릴 때)."""
import json
from pathlib import Path

from minutes.correction import CORRECTION_KIND
from minutes.placement import PROMPTS_DIR, load_prompt
from minutes.polish import POLISH_KIND
from minutes.relayout import previous_lines_from
from shared.llm import Reply

FIXTURE = Path(__file__).parent / "fixtures" / "draft_request.json"
ITEMS = ["안건별 논의", "결정 사항", "미결 안건", "할 일"]

# 오인식 교정 지시문들. 가짜 AI 가 교정 호출을 알아보는 데 쓴다
CORRECTION_TEXTS = {load_prompt(p.stem, CORRECTION_KIND).text for p in (PROMPTS_DIR / CORRECTION_KIND).glob("v*.md")}
NO_FIXES = {"fixes": []}
# 문장 다듬기 지시문들 (교정과 같은 방식으로 따로 다룬다). 답이 없으면 모든 줄이 ① 의 글이 된다
POLISH_TEXTS = {load_prompt(p.stem, POLISH_KIND).text for p in (PROMPTS_DIR / POLISH_KIND).glob("v*.md")}
NO_POLISH = {"lines": []}

# 합성 요청에 대한 맞는 답. AI 는 칸만 고른다 (글은 코드가 정함)
GOOD = {
    "placements": [
        {"m": "m1", "section": 2},
        {"m": "m2", "section": 1}, {"m": "m3", "section": 1},
        {"m": "m4", "section": 4}, {"m": "m5", "section": 3}, {"m": "m6", "section": 1},
        {"m": "m7", "section": 4}, {"m": "m8", "section": 3}, {"m": "m10", "section": 4},
    ],
    "unplaced": ["m9"],
}


def load_request() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ScriptLLM:
    """정해 둔 답(dict·글 또는 예외)을 차례로 낸다. 받은 입력을 기록한다.
    오인식 교정 호출은 따로 다룬다: corrections 의 답을 차례로 내고, 다 쓰면 "찾은 것 없음". 입력은 correction_calls 에 남긴다
    (그래서 칸 고르기·표 채우기 시험은 교정이 생기기 전처럼 replies 와 calls 만 보면 된다).
    문장 다듬기 호출도 같다: polishes 의 답을 차례로 내고, 다 쓰면 빈 답(모든 줄이 ① 의 글). 입력은 polish_calls 에 남긴다."""
    name = "script"
    model = "script"

    def __init__(self, *replies, corrections=(), polishes=()):
        self.replies, self.calls = list(replies), []
        self.corrections, self.correction_calls = list(corrections), []
        self.polishes, self.polish_calls = list(polishes), []

    def generate(self, system, user, timeout_s, effort=None):
        self.efforts = getattr(self, "efforts", []) + [effort]
        if system in CORRECTION_TEXTS:
            self.correction_calls.append((system, user, timeout_s))
            r = self.corrections.pop(0) if self.corrections else NO_FIXES
        elif system in POLISH_TEXTS:
            self.polish_calls.append((system, user, timeout_s))
            r = self.polishes.pop(0) if self.polishes else NO_POLISH
        else:
            self.calls.append((system, user, timeout_s))
            r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Reply(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False), 100, 20)

# 처음 초안 결과 → previousLines. 시험에서 줄을 고치거나 더해 쓴다
previous_lines_of = previous_lines_from
