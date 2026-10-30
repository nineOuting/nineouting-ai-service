"""검색 평가 도구의 지표 계산 시험 (모델·DB 필요 없음)."""
import pytest

import json
from pathlib import Path

from rag.eval_search import at_location, gold_for, gold_indexes, load_chunks, score, summarize, table
from rag.models import Chunk


def chunk(text):
    return Chunk(0, text, text, {"type": "time", "label": "00:00~00:01"}, len(text))


def test_순위로_적중률_재현율_MRR():
    order = [7, 3, 9, 1, 4, 8]                       # 조각 번호를 순위대로
    s = score(order, golds=[[3], [8]], ks=[1, 5])    # 정답 3 은 2위, 8 은 6위
    assert s["ranks"] == [2, 6] and s["mrr"] == 0.5
    assert s["hit@1"] == 0.0 and s["hit@5"] == 1.0
    assert s["recall@1"] == 0.0 and s["recall@5"] == 0.5


def test_유형별과_전체_평균():
    rows = [("가", {"hit@5": 1.0, "recall@5": 1.0, "mrr": 1.0}),
            ("가", {"hit@5": 0.0, "recall@5": 0.0, "mrr": 0.1}),
            ("나", {"hit@5": 1.0, "recall@5": 0.5, "mrr": 0.5})]
    s = summarize(rows, [5])
    assert s["가"]["n"] == 2 and s["가"]["hit@5"] == 0.5
    assert s["전체"]["n"] == 3 and abs(s["전체"]["recall@5"] - 0.5) < 1e-9
    t = table(s, [5])
    assert t.splitlines()[-1].startswith("| 전체 | 3 |")         # 전체는 맨 아래


def test_정답_조각은_정확히_하나여야_함():
    chunks = [("a", chunk("베타 참여자는 100명")), ("a", chunk("베타 기간은 2주")), ("b", chunk("베타 참여자는 100명"))]
    assert gold_indexes(chunks, [{"doc": "b", "starts": "베타 참여자"}]) == [[2]]
    with pytest.raises(ValueError):
        gold_indexes(chunks, [{"doc": "a", "starts": "베타"}])          # 둘이 걸림
    with pytest.raises(ValueError):
        gold_indexes(chunks, [{"doc": "a", "starts": "없는 글"}])


def test_정답_위치가_여러_조각이면_가장_높은_순위():
    order = [5, 2, 9, 7]
    s = score(order, golds=[[7, 2], [9]], ks=[1, 3])    # 첫 정답은 7(4위)·2(2위) 중 2위, 둘째는 3위
    assert s["ranks"] == [2, 3] and s["mrr"] == 0.5 and s["recall@3"] == 1.0


def test_위치로_맞추기():
    assert at_location({"type": "page", "page": 4, "label": "4쪽"}, {"page": 4})
    assert not at_location({"type": "page", "page": 3, "label": "3쪽"}, {"page": 4})
    assert at_location({"type": "section", "heading": "7. 예산", "label": "7. 예산"}, {"heading": "7. 예산"})
    sheet = {"type": "sheet", "sheet": "7. 예산", "rows": [2, 9], "label": "7. 예산 시트 2~9행"}
    assert at_location(sheet, {"sheet": "7. 예산", "row": 9})
    assert not at_location(sheet, {"sheet": "7. 예산", "row": 10})
    assert not at_location(sheet, {"sheet": "5. 준비물과 장비", "row": 4})


def test_형식에_정답_위치가_없으면_뺌():
    gold = [{"doc": "plan", "pdf": {"page": 4}, "xlsx": {"sheet": "7. 예산", "row": 4}},
            {"doc": "plan", "pdf": {"page": 6}}]
    assert len(gold_for(gold, "pdf")) == 2
    assert len(gold_for(gold, "xlsx")) == 1
    assert gold_for(gold, "docx") == []
    assert gold_for([{"doc": "a", "starts": "베타"}], None) == [{"doc": "a", "starts": "베타"}]


ROOT = Path(__file__).resolve().parents[2]
PLAN = json.loads((ROOT / "rag/tests/fixtures/eval_plan.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fmt", ["pdf", "docx", "xlsx"])
def test_운영계획서_질문의_정답_위치가_샘플에_있음(fmt, monkeypatch):
    """샘플을 다시 만들거나 로더를 바꿔 위치가 달라지면 여기서 걸린다. 모델 없이 글자 수로 나눈다."""
    monkeypatch.chdir(ROOT)
    chunks = load_chunks(PLAN["docs"], fmt, len, 512)
    scored = 0
    for q in PLAN["questions"]:
        gold = gold_for(q["gold"], fmt)
        for g, hit in zip(gold, gold_indexes(chunks, gold, fmt)):
            texts = "".join(chunks[i][1].content for i in hit).replace("\n", "").replace(" ", "")
            # 14번 엑셀은 문단 대신 합계 행이 정답이라 answer 글귀가 없다
            assert g["answer"].replace(" ", "") in texts or (q["id"] == 14 and fmt == "xlsx"), (q["id"], g)
        scored += bool(gold)
    assert scored == {"pdf": 35, "docx": 35, "xlsx": 22}[fmt]
