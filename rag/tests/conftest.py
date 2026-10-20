"""공용 시험 도구: 가짜 임베더, 시험 DB 연결.

DB 시험은 RAG_TEST_DATABASE_URL 의 DB 를 쓰고 시험마다 표를 비운다. 개발 DB(RAG_DATABASE_URL)는 건드리지 않는다.
DB 에 접속할 수 없으면 건너뛰고, RAG_REQUIRE_DB=1 이면 실패시킨다.
"""
import hashlib
import os

import numpy as np
import pytest

from rag.config import EMBED_DIM, MAX_TOKENS


class FakeEmbedder:
    """글자로 정해지는 결정적 벡터. 같은 글이면 같은 벡터라, 조각 글 그대로 물으면 그 조각이 1위다."""
    model_id = "fake@0"
    dim = EMBED_DIM
    max_tokens = MAX_TOKENS

    def passage_tokens(self, text):
        return len(text)

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    def _vec(self, text):
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        v = np.random.default_rng(seed).standard_normal(EMBED_DIM)
        return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()


def _no_db(reason):
    msg = f"시험 DB 없음: {reason}. rag/README.md 의 'DB' 를 본다"
    if os.environ.get("RAG_REQUIRE_DB") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="session")
def _db_session():
    if not os.environ.get("RAG_TEST_DATABASE_URL"):
        _no_db("RAG_TEST_DATABASE_URL 이 없음")
    try:
        import psycopg
        from rag.db import connect
        from rag.migrate import migrate
    except ImportError as e:
        _no_db(f"패키지 없음 ({e.name})")
    try:
        conn = connect(env="RAG_TEST_DATABASE_URL")
    except psycopg.OperationalError as e:
        _no_db(f"접속 실패 ({type(e).__name__})")      # 메시지에 주소가 섞일 수 있어 예외 이름만
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def conn(_db_session):
    _db_session.execute("TRUNCATE rag_chunks, rag_documents, rag_jobs RESTART IDENTITY")
    return _db_session


@pytest.fixture
def other_conn(conn):
    """동시성 시험용 두 번째 연결 (같은 시험 DB)."""
    from rag.db import connect
    c = connect(env="RAG_TEST_DATABASE_URL")
    yield c
    c.close()
