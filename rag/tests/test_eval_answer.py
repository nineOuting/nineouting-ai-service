"""답변 지시문 평가 도구 시험. AI 는 가짜 (비용 없음)."""
import json
from pathlib import Path

import pytest

from rag import eval_answer as ev
from rag.answer import answer_prompt
from shared.llm import LLMTimeout, Reply

FIX = Path(__file__).parent / "fixtures"
CASES = sorted(FIX.glob("answer_eval_*.json"))


class ScriptLLM:
    """질문 글에 따라 정해 둔 답을 낸다. 답이 예외면 던진다."""
    name, model = "script", "script-1"

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def generate(self, system, user, timeout_s):
        self.calls.append((system, user))
        for q, r in self.replies.items():
            if f"<질문>\n{q}\n</질문>" in user:
                if isinstance(r, Exception):
                    raise r
                return Reply(r, 700, 90)
        return Reply("NO_ANSWER", 700, 1)


@pytest.fixture(scope="module")
def two():
    return ev.load_case(FIX / "answer_eval_two_meetings.json")


def q_of(case, qid):
    return next(q for q in case["questions"] if q["id"] == qid)


# ---- 사례 파일 ----

def test_사례_파일이_있음():
    assert {p.name for p in CASES} >= {"answer_eval_basic.json", "answer_eval_two_meetings.json"}


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_사례_파일의_조각과_정답표가_맞물림(path):
    case = ev.load_case(path)
    assert [x for q in case["questions"] for x in ev.check_question(case, q)] == []
    assert ev.dry_run(case, out=lambda *_: None)


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_사례_조각은_AI에_가는_글에_번호대로_들어감(path):
    case = ev.load_case(path)
    for q in case["questions"]:
        hits = ev.hits_for(case, q)
        user = ev.build_prompt(q["q"], hits)
        for i, (doc, needle) in enumerate(q["pieces"], 1):
            assert f"[{i}] ({case['docs'][doc]['title']}," in user
            assert needle in hits[i - 1].content


def test_조각을_하나로_고를_수_없으면_사례_오류(two):
    q = {**q_of(two, "D1"), "pieces": [["A", "배포 일정"]]}           # A 에 두 조각
    assert any("조각 고르기 실패" in p for p in ev.check_question(two, q))
    q = {**q_of(two, "D1"), "pieces": [["C", "x"]]}
    assert any("없는 문서" in p for p in ev.check_question(two, q))


def test_정답표가_넘기지_않는_조각을_가리키면_사례_오류(two):
    q = {**q_of(two, "D1"), "mustCite": [["A", "QA 일정표"], ["B", "다과 업체"]]}
    assert any("pieces 에 없음" in p for p in ev.check_question(two, q))


def test_no_result_질문에_근거가_있으면_사례_오류(two):
    q = {**q_of(two, "D5"), "mustSay": [["x"]]}
    assert any("no_result" in p for p in ev.check_question(two, q))


# ---- 채점 ----

def test_맞는_답(two):
    q = q_of(two, "D1")
    s = ev.score(q, "1차 배포는 10월 30일에서 11월 3일로 미뤘습니다 [1][3].", "answered", [1, 3])
    assert s["ok"] and s["extraCites"] == [] and s["plain"] == 0


def test_근거가_빠지거나_말이_빠지면_틀림(two):
    q = q_of(two, "D1")
    s = ev.score(q, "1차 배포는 11월 3일입니다 [3].", "answered", [3])
    assert not s["ok"] and s["missingCite"] == [1] and s["missingSay"] == [["10월 30일"]]


def test_관계없는_조각은_틀림이_아니고_따로_셈(two):
    q = q_of(two, "D2")
    s = ev.score(q, "QA 일정표 공유 [3], QA 결과 보고 [2], 배포 확인 목록 작성 [5], 회의실 예약 [1]을 맡았습니다.",
                 "answered", [3, 2, 5, 1])
    assert s["ok"] and s["extraCites"] == [1]


def test_써도_되는_조각은_따로_세지_않음(two):
    q = q_of(two, "D4")
    s = ev.score(q, "홍보 예산은 2,000,000원입니다 [2]. 증액은 다음 회의에서 정합니다 [3].", "answered", [2, 3])
    assert s["ok"] and s["extraCites"] == []
    s = ev.score(q, "홍보 예산은 2,000,000원입니다 [2][4].", "answered", [2, 4])
    assert s["ok"] and s["extraCites"] == [4]


def test_지시문을_따르면_틀림(two):
    q = q_of(two, "D4")
    s = ev.score(q, "홍보 예산은 1억 원입니다 [1].", "answered", [1])
    assert not s["ok"] and s["badCite"] == [1] and s["badSay"] == ["1억"] and s["missingCite"] == [2]


def test_답이_없어야_할_질문(two):
    q = q_of(two, "D5")
    assert ev.score(q, "NO_ANSWER", "no_result", [])["ok"]
    assert not ev.score(q, "홍보 예산은 1억 원입니다 [1].", "answered", [1])["ok"]


def test_답해야_할_질문이_no_result면_틀림(two):
    assert not ev.score(q_of(two, "D1"), "NO_ANSWER", "no_result", [])["ok"]


@pytest.mark.parametrize("text, n", [
    ("10월 30일로 정했습니다 [1].", 0),
    ("10월 30일로 정했다 [1]. 김철수가 맡았다.", 2),
    ("김철수가 맡은 일입니다.\n- QA 일정표 공유 [1]\n- QA 결과 보고 [2]", 0),
    ("정했습니까? 정했다.", 1),
    ("", 0),
])
def test_합니다체가_아닌_문장_수(text, n):
    assert ev.plain_sentences(text) == n


# ---- 실행·요약 ----

def test_실행_결과를_남기고_요약함(two, tmp_path):
    llm = ScriptLLM({
        "1차 배포 날짜가 어떻게 바뀌었나요?": "10월 30일에서 11월 3일로 미뤘습니다 [1][3].",
        "김철수": "QA 일정표 공유 [3]를 맡았습니다.",                          # 근거·말 빠짐
        "다음 회의 장소와 참석 인원은 어떻게 되나요?": LLMTimeout("시간 초과"),
        "홍보 예산은 얼마인가요?": "홍보 예산은 2,000,000원이다 [2].",           # 맞지만 합니다체 아님
    })
    out = ev.run_case(two, llm, 2, ["v3"], tmp_path / "out", progress=lambda *_: None)
    rows = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 * 5 and len(llm.calls) == 10
    by = {r["question"]: r for r in rows if r["run"] == 1}
    assert by["D1"]["score"]["ok"] and by["D4"]["score"]["ok"] and by["D5"]["score"]["ok"]
    assert by["D2"]["score"]["missingCite"] == [2, 5]
    assert by["D3"]["score"]["status"] == "failed" and by["D3"]["score"]["error"] == "LLMTimeout"
    assert by["D3"]["calls"][0]["error"] == "LLMTimeout"
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["variants"] == ["answer-v3"] and meta["model"] == "script/script-1"
    assert len((out / "calls.csv").read_text(encoding="utf-8").splitlines()) == 1 + 10

    text = ev.summarize(out)
    assert "| D1 | answer-v3 | 2/2 | 2 / 0 / 0 | 0 | 0 |" in text
    assert "| D4 | answer-v3 | 2/2 | 2 / 0 / 0 | 0 | 2 |" in text
    assert "- D2 / answer-v3: 근거 빠짐 [2, 5] (2번)" in text
    assert "- D3 / answer-v3: failed LLMTimeout (2번)" in text
    assert "| answer-v3 | 6/10 |" in text


def test_버전을_판마다_번갈아_돌림(two, tmp_path):
    llm = ScriptLLM({})
    out = ev.run_case(two, llm, 2, ["v2", "v3"], tmp_path / "out", progress=lambda *_: None)
    rows = [json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["run"], r["variant"]) for r in rows[::5]] == [(1, "answer-v2"), (1, "answer-v3"),
                                                              (2, "answer-v2"), (2, "answer-v3")]
    systems = [s for s, _ in llm.calls]
    assert systems[0] == answer_prompt("v2").text and systems[5] == answer_prompt("v3").text


def test_저장된_답을_고친_정답표로_다시_채점(two, tmp_path):
    llm = ScriptLLM({"김철수": "QA 일정표 공유 [3], QA 결과를 보고 [2], 배포 확인 목록 작성 [5]을 맡았습니다.",
                     "다음 회의 장소와 참석 인원은 어떻게 되나요?": LLMTimeout("시간 초과")})
    out = ev.run_case(two, llm, 1, ["v3"], tmp_path / "out", progress=lambda *_: None)
    strict = {**two, "questions": [{**q, "mustSay": [["QA 결과 보고"]]} if q["id"] == "D2" else q
                                   for q in two["questions"]]}
    assert "| D2 | answer-v3 | 1/1 |" in ev.summarize(out)
    text = ev.summarize(out, strict)
    assert "| D2 | answer-v3 | 0/1 |" in text and "- D3 / answer-v3: failed LLMTimeout (1번)" in text
    before = (out / "runs.jsonl").read_text(encoding="utf-8")
    ev.main([str(FIX / "answer_eval_two_meetings.json"), "--rescore", str(out)])
    assert (out / "runs.jsonl").read_text(encoding="utf-8") == before   # 결과 파일은 그대로


def test_범위_밖_번호는_failed(two, tmp_path):
    llm = ScriptLLM({"1차 배포 날짜가 어떻게 바뀌었나요?": "11월 3일입니다 [9]."})
    out = ev.run_case(two, llm, 1, ["v3"], tmp_path / "out", progress=lambda *_: None)
    r = json.loads((out / "runs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert r["score"]["status"] == "failed" and not r["score"]["ok"]
    assert "- D1 / answer-v3: failed (범위 밖 번호) (1번)" in ev.summarize(out)


def test_가짜_AI_설정이면_실제_실행을_멈춤(monkeypatch, tmp_path):
    import shared.envfile
    monkeypatch.setattr(shared.envfile, "ROOT_ENV", tmp_path / "없는.env")
    monkeypatch.setattr(shared.envfile, "OLD_ENV", tmp_path / "없는.env")
    monkeypatch.setenv("RAG_LLM_PROVIDER", "fake")
    with pytest.raises(SystemExit, match="fake"):
        ev.real_llm()
