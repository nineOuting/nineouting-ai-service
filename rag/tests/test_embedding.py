"""실제 모델을 쓰는 시험. 캐시에 모델이 없으면 건너뛴다 (내려받지 않는다).
RAG_REQUIRE_MODEL=1 이면 건너뛰지 않고 실패시킨다 (CI 용)."""
import math
import os

import pytest

from rag.chunking import split_pieces
from rag.config import EMBED_DIM, MAX_TOKENS
from rag.models import Piece


def _no_model(e):
    msg = f"모델 없음: `.venv/bin/python -m rag.fetch_model` 로 받는다 ({type(e).__name__})"
    if os.environ.get("RAG_REQUIRE_MODEL") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="module")
def embedder():
    from rag.embedding import E5Embedder
    try:
        return E5Embedder(threads=2)
    except OSError as e:
        _no_model(e)


def test_모델_파일_체크섬():
    from rag.embedding import verify_model_files
    try:
        verify_model_files()
    except OSError as e:
        _no_model(e)


def test_차원과_정규화와_결정성(embedder):
    a, b = embedder.embed_passages(["회의 일정은 금요일", "회의 일정은 금요일"])
    assert len(a) == EMBED_DIM
    assert math.isclose(math.fsum(x * x for x in a), 1.0, abs_tol=1e-4)
    assert max(abs(x - y) for x, y in zip(a, b)) < 1e-5
    assert embedder.model_id.startswith("dragonkue/multilingual-e5-small-ko@bba4cb8e")


def test_질문과_조각은_접두어가_달라_벡터도_다름(embedder):
    q = embedder.embed_query("회의 일정은 금요일")
    p = embedder.embed_passages(["회의 일정은 금요일"])[0]
    assert max(abs(x - y) for x, y in zip(q, p)) > 1e-3


def test_토큰_수에_접두어와_특수_토큰_포함(embedder):
    assert embedder.passage_tokens("") >= 3                # passage: + 시작·끝 특수 토큰


def test_실제_토크나이저로_나눈_조각은_상한_안(embedder):
    text = "다음 스프린트부터 일정 관리는 Jira로 옮기기로 했습니다. " * 60
    chunks = split_pieces([Piece(text, {"type": "time"}, context="도구")], embedder.passage_tokens, MAX_TOKENS)
    assert len(chunks) > 1
    assert all(c.token_count <= MAX_TOKENS for c in chunks)
    embedder.embed_passages([c.embed_text for c in chunks])  # 상한을 넘으면 ValueError


def test_상한을_넘는_조각은_거부(embedder):
    with pytest.raises(ValueError):
        embedder.embed_passages(["일정 " * 600])


def test_관련_있는_조각이_더_가까움(embedder):
    q = embedder.embed_query("일정 관리 도구는 무엇으로 바꾸나요?")
    jira, lunch = embedder.embed_passages(["[도구] 다음 스프린트부터 일정 관리는 Jira로 옮기기로 함",
                                           "[잡담] 점심은 근처 국밥집에서 먹기로 함"])
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(q, jira) > dot(q, lunch)


def test_여러_스레드가_같이_써도_오류_없음(embedder):
    """작업자와 답변 API 가 임베더 하나를 같이 쓴다. 토큰 세기·임베딩을 동시에 불러도 같은 결과여야 한다."""
    from concurrent.futures import ThreadPoolExecutor

    want_q = embedder.embed_query("회의 일정")
    want_n = embedder.passage_tokens("회의 일정은 금요일")

    def work(i):
        if i % 2:
            return embedder.embed_query("회의 일정") == want_q
        return embedder.passage_tokens("회의 일정은 금요일") == want_n and len(embedder.embed_passages(["가", "나"])) == 2

    with ThreadPoolExecutor(max_workers=8) as ex:
        assert all(ex.map(work, range(64)))
