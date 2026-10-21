"""검색 평가 도구의 지표 계산 시험 (모델·DB 필요 없음)."""
import pytest

from rag.eval_search import gold_indexes, score, summarize, table
from rag.models import Chunk


def chunk(text):
    return Chunk(0, text, text, {"type": "time", "label": "00:00~00:01"}, len(text))


def test_순위로_적중률_재현율_MRR():
    order = [7, 3, 9, 1, 4, 8]                       # 조각 번호를 순위대로
    s = score(order, golds=[3, 8], ks=[1, 5])        # 정답 3 은 2위, 8 은 6위
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
    assert gold_indexes(chunks, [{"doc": "b", "starts": "베타 참여자"}]) == [2]
    with pytest.raises(ValueError):
        gold_indexes(chunks, [{"doc": "a", "starts": "베타"}])          # 둘이 걸림
    with pytest.raises(ValueError):
        gold_indexes(chunks, [{"doc": "a", "starts": "없는 글"}])
