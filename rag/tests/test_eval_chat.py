"""챗봇 평가 도구 시험. 임베딩 모델과 AI 없이 사례 파일 점검과 채점만 본다."""
from pathlib import Path

from rag.eval_chat import (check_case, hit, live_all_texts, live_gold_refs, live_request, load_case, rr, says,
                           score)
from rag.chat import live_evidence
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
    c = {"expect": "answered", "gold": [["A", "수요일"]], "mustSay": [["수요일"]], "mustNotSay": ["1억"]}
    assert score(c, "수요일까지예요 [2].", "answered", [2], [2])["ok"]
    assert not score(c, "수요일까지예요 [1].", "answered", [1], [2])["ok"]          # 정답 조각을 쓰지 않음
    assert not score(c, "금요일까지예요 [2].", "answered", [2], [2])["ok"]          # 말 빠짐
    assert not score(c, "수요일, 1억 [2].", "answered", [2], [2])["ok"]             # 나오면 안 될 말
    assert score({"expect": "no_result"}, "", "no_result", [], [])["ok"]
    both = {"expect": ["answered", "no_result"], "mustNotSay": ["윤가영"]}            # 둘 다 맞고 gold 없음
    assert score(both, "", "no_result", [], [])["ok"] and score(both, "누가 했는지는 몰라요 [1].", "answered", [1], [])["ok"]
    assert not score(both, "윤가영 님이에요 [1].", "answered", [1], [])["ok"]


def test_검색_지표():
    assert hit([7, 3]) and not hit([6, 9])
    assert rr([4, 2]) == 0.5 and rr([]) == 0.0


def test_숫자로_시작하는_말은_앞에_숫자가_붙으면_다른_말():
    assert not says("11월 5일 회의에서 그대로 두기로 했어요", "1월 5일")
    assert says("출시일은 1월 5일이에요", "1월 5일") and says("(1월 5일)", "1월 5일")
    assert says("2,000,000원", "2,000,000") and not says("12,000,000원", "2,000,000")
    assert says("B사 연동", "B사")
    c = {"expect": "answered", "mustSay": [["12월 8일"]], "mustNotSay": ["1월 5일"]}
    assert score(c, "12월 8일로 정했고 11월 5일 회의에서도 그대로예요 [1].", "answered", [1], [1])["ok"]


LIVE_CASE = Path(__file__).parent / "fixtures" / "chat_eval_live.json"


def test_진행_중_회의_사례_파일이_점검을_통과함():
    case = load_case(LIVE_CASE)
    chunks = load_chunks(case["docs"], None, len, 256)
    assert [p for c in case["cases"] for p in check_case(chunks, c, live_all_texts(case, c))] == []
    assert case["liveData"]["currentAgendaId"] == 3


def test_진행_중_회의_정답은_넘긴_근거_안에서_번호를_찾음():
    case = load_case(LIVE_CASE)
    c = {"id": "X", "q": "출시일", "gold": [["LIVE", "12월 15일(월)로"]]}
    board, lines = live_evidence(live_request(case, c, c["q"]))
    refs = live_gold_refs(c, board + lines)
    assert len(refs) == 1 and "12월 15일" in (board + lines)[refs[0] - 1].text
    assert live_evidence(live_request(case, {**c, "noLive": True}, c["q"])) == ([], [])


SUMMARY_CASE = Path(__file__).parent / "fixtures" / "chat_eval_live_summary.json"


def test_실시간_요약_사례_파일이_점검을_통과하고_요약은_빼고_돌릴_수_있음():
    case = load_case(SUMMARY_CASE)
    chunks = load_chunks(case["docs"], None, len, 256)
    assert [p for c in case["cases"] for p in check_case(chunks, c, live_all_texts(case, c))] == []
    c = case["cases"][0]
    assert len(live_request(case, c, c["q"]).live_summary) == 75
    assert live_request(case, {**c, "noSummary": True}, c["q"]).live_summary == ()
    assert live_request({**case, "noSummaryAll": True}, c, c["q"]).live_summary == ()


def test_진행_중_회의_정답이_없거나_둘이면_문제로_잡음():
    case = load_case(LIVE_CASE)
    chunks = load_chunks(case["docs"], None, len, 256)
    for needle in ("없는 말", "다크 모드"):
        c = {"id": "X", "q": "?", "expect": "answered", "gold": [["LIVE", needle]]}
        assert any("진행 중 회의 정답 고르기 실패" in p for p in check_case(chunks, c, live_all_texts(case, c)))



def test_근거_종류_채점과_general_사례_점검():
    c = {"id": "X", "q": "?", "expect": "answered", "basis": "general", "mustSay": [["비교"]]}
    assert score(c, "두 안을 비교해요 [일반].", "answered", [], [], "general")["ok"]
    bad = score(c, "두 안을 비교해요 [1].", "answered", [1], [], "sources")
    assert not bad["ok"] and bad["badBasis"]
    assert check_case([], c) == []
    assert any("gold 가 있음" in p for p in check_case([], {**c, "gold": [["LIVE", "x"]]}))
    assert any("basis 는" in p for p in check_case([], {**c, "basis": "other"}))
