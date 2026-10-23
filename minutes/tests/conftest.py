"""회의록 초안 시험의 DB 준비.

DB 시험은 MINUTES_TEST_DATABASE_URL(없으면 RAG_TEST_DATABASE_URL)의 DB 를 쓰고, 시험마다 minutes_jobs 표만 비운다.
RAG 표와 개발 DB 는 건드리지 않는다. DB 에 접속할 수 없으면 건너뛰고, MINUTES_REQUIRE_DB=1 이면 실패시킨다.
"""
import os

import pytest


def _no_db(reason):
    msg = f"시험 DB 없음: {reason}"
    if os.environ.get("MINUTES_REQUIRE_DB") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="session")
def _db_session():
    try:
        import psycopg
        from minutes.db import connect, database_url_for_tests
        from minutes.migrate import migrate
    except ImportError as e:
        _no_db(f"패키지 없음 ({e.name})")
    url = database_url_for_tests()
    if not url:
        _no_db("MINUTES_TEST_DATABASE_URL 이나 RAG_TEST_DATABASE_URL 이 없음")
    try:
        conn = connect(url=url)
    except psycopg.OperationalError as e:
        _no_db(f"접속 실패 ({type(e).__name__})")      # 메시지에 주소가 섞일 수 있어 예외 이름만
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def conn(_db_session):
    _db_session.execute("TRUNCATE minutes_jobs RESTART IDENTITY")
    return _db_session


@pytest.fixture
def other_conn(conn):
    """동시성 시험용 두 번째 연결 (같은 시험 DB)."""
    from minutes.db import connect, database_url_for_tests
    c = connect(url=database_url_for_tests())
    yield c
    c.close()
