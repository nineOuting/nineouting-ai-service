"""검색 시험. 빈 docIds 시험만 DB 없이 돈다."""
import json
from pathlib import Path

import pytest

from _helpers import OTHER_WS, WS, doc, put
from rag.search import MAX_TOP_K, search


def test_빈_docIds_는_DB_에_묻지_않고_빈_목록(fake_embedder):
    assert search(None, fake_embedder, WS, [], "아무 질문") == []      # conn=None 이라 DB 를 쓰면 오류가 난다


def test_조각_글_그대로_물으면_그_조각이_1위(conn, fake_embedder):
    put(conn, fake_embedder, doc(1), "회의는 금요일", "점심은 국밥")
    hits = search(conn, fake_embedder, WS, [1], "점심은 국밥")
    assert hits[0].content == "점심은 국밥"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert (hits[0].doc_id, hits[0].source_type, hits[0].title) == (1, "minutes", "주간 회의")
    assert hits[0].location["label"] == "00:00~00:01"


def test_다른_워크스페이스는_같은_docId_를_넘겨도_안_나옴(conn, fake_embedder):
    put(conn, fake_embedder, doc(1, ws=WS), "가")
    assert search(conn, fake_embedder, OTHER_WS, [1], "가") == []


def test_넘긴_docIds_안에서만_찾음(conn, fake_embedder):
    put(conn, fake_embedder, doc(1), "가")
    put(conn, fake_embedder, doc(2), "나")
    hits = search(conn, fake_embedder, WS, [2], "가")
    assert {h.doc_id for h in hits} == {2}


def test_다른_모델로_만든_문서는_안_나옴(conn, fake_embedder):
    put(conn, fake_embedder, doc(1), "가")
    conn.execute("UPDATE rag_documents SET embed_model = 'other@1'")
    assert search(conn, fake_embedder, WS, [1], "가") == []


def test_topK_만큼만_돌려주고_범위를_검사(conn, fake_embedder):
    put(conn, fake_embedder, doc(1), *[f"글 {i}" for i in range(8)])
    assert len(search(conn, fake_embedder, WS, [1], "글 3", top_k=3)) == 3
    for bad in (0, MAX_TOP_K + 1):
        with pytest.raises(ValueError):
            search(conn, fake_embedder, WS, [1], "글", top_k=bad)


def test_실제_모델로_회의록을_넣고_찾기(conn):
    """로더 -> 나누기 -> 임베딩 -> 저장 -> 검색 을 끝까지. 기본 확인이며 품질 측정은 아니다."""
    from rag.chunking import split_pieces
    from rag.embedding import E5Embedder
    from rag.loaders.minutes import load_minutes
    from rag.store import register, store

    try:
        emb = E5Embedder(threads=2)
    except OSError:
        pytest.skip("모델 없음: `.venv/bin/python -m rag.fetch_model` 로 받는다")
    minutes = json.loads((Path(__file__).parent / "fixtures" / "minutes_basic.json").read_text(encoding="utf-8"))
    chunks = split_pieces(load_minutes(minutes).pieces, emb.passage_tokens, emb.max_tokens)
    register(conn, doc(1))
    store(conn, 1, "v1", chunks, emb.embed_passages([c.embed_text for c in chunks]), emb.model_id)

    hits = search(conn, emb, WS, [1], "일정 관리 도구는 무엇으로 바꾸나요?")
    assert "Jira" in hits[0].content
    assert hits[0].location["label"] == "05:12~05:40"
