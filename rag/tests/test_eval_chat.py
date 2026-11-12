"""챗봇 평가 도구 시험. 임베딩 모델과 AI 없이 사례 파일 점검과 채점만 본다."""
from pathlib import Path

from rag.eval_chat import check_case, hit, load_case, rr, score
from rag.eval_search import load_chunks

CASE = Path(__file__).parent / "fixtures" / "chat_eval_two_meetings.json"


def test_사례_파일이_점검을_통과함():
    case = load_case(CASE)
    chunks = load_chunks(case["docs"], None, len, 256)
    assert [p for c in case["cases"] for p in check_case(chunks, c)] == []
    assert len(case["cases"]) == 12 and {c["expect"] for c in case["cases"]} == {"answered", "no_result"}


def test_정답_조각이_둘이거나_없으면_문제로_잡음():
    case = load_case(CASE)
    chunks = load_chunks(case["docs"], None, len, 256)
    bad = {"id": "X", "q": "?", "expect": "answered", "gold": [["A", "할 일"]]}
    assert any("정답 조각 고르기 실패" in p for p in check_case(chunks, bad))
    assert any("gold 가 없음" in p for p in check_case(chunks, {"id": "Y", "q": "?", "expect": "answered"}))


def test_채점():
    c = {"expect": "answered", "mustSay": [["수요일"]], "mustNotSay": ["1억"]}
    assert score(c, "수요일까지예요 [2].", "answered", [2], [2])["ok"]
    assert not score(c, "수요일까지예요 [1].", "answered", [1], [2])["ok"]          # 정답 조각을 쓰지 않음
    assert not score(c, "금요일까지예요 [2].", "answered", [2], [2])["ok"]          # 말 빠짐
    assert not score(c, "수요일, 1억 [2].", "answered", [2], [2])["ok"]             # 나오면 안 될 말
    assert score({"expect": "no_result"}, "", "no_result", [], [])["ok"]


def test_검색_지표():
    assert hit([7, 3]) and not hit([6, 9])
    assert rr([4, 2]) == 0.5 and rr([]) == 0.0
