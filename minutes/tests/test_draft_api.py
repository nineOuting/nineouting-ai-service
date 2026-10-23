"""초안 접수 주소와 런타임 시험 (시험 DB 필요). 작은 앱에 라우터만 붙인다.
rag/tests/test_api.py 와 이름이 겹치지 않게 따로 이름을 붙였다."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI                     # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

from minutes.api import make_router            # noqa: E402
from minutes.db import database_url_for_tests       # noqa: E402
from minutes.runtime import setup              # noqa: E402
from minutes_fakes import load_request         # noqa: E402

URL = "/internal/minutes/drafts"
ALLOWED = {"minutes-service:8084"}              # 합성 요청의 callbackUrl 호스트


def make_pool(url, timeout=5.0):
    from psycopg_pool import ConnectionPool
    return ConnectionPool(url, min_size=1, max_size=2, timeout=timeout, open=True, kwargs={"autocommit": True},
                          name="minutes-test")


def app_with(pool, **kw):
    app = FastAPI()
    app.include_router(make_router(pool, allowed_callback_hosts=ALLOWED, **kw))
    return TestClient(app)


@pytest.fixture
def client(conn):                                # conn: 표를 비우고, DB 가 없으면 건너뛰게 한다
    pool = make_pool(database_url_for_tests())
    with app_with(pool) as c:
        yield c
    pool.close()


def rows(conn):
    return conn.execute("SELECT request_id, status, trace_id FROM minutes_jobs ORDER BY id").fetchall()


def test_접수하면_202_ACCEPTED이고_작업이_생긴다(client, conn):
    body = load_request()
    r = client.post(URL, json=body, headers={"X-Request-Id": "t-1"})
    assert r.status_code == 202 and r.json() == {"requestId": body["requestId"], "status": "ACCEPTED"}
    assert rows(conn) == [(body["requestId"], "queued", "t-1")]


def test_같은_요청을_다시_보내면_202_DUPLICATE이고_작업은_하나(client, conn):
    body = load_request()
    client.post(URL, json=body)
    r = client.post(URL, json=body)
    assert r.status_code == 202 and r.json()["status"] == "DUPLICATE"
    assert len(rows(conn)) == 1


def test_추적_번호는_200자까지만_남긴다(client, conn):
    client.post(URL, json=load_request(), headers={"X-Request-Id": "t" * 300})
    assert len(rows(conn)[0][2]) == 200


@pytest.mark.parametrize("change, word", [
    (lambda b: b.update(template={"items": []}), "template.items"),
    (lambda b: b.pop("requestId"), "requestId"),
    (lambda b: b.update(callbackUrl="http://evil.example/x"), "허용 목록"),
    (lambda b: b.update(callbackUrl="ftp://minutes-service:8084/x"), "http 또는 https"),
    (lambda b: b.update(previousLines=[{"title": "할 일"}]), "준비 중"),
])
def test_틀린_요청은_400_INVALID_INPUT이고_아무것도_남기지_않는다(client, conn, change, word):
    body = load_request()
    change(body)
    r = client.post(URL, json=body)
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT" and word in r.json()["detail"]
    assert rows(conn) == []


def test_본문이_객체가_아니면_400(client):
    r = client.post(URL, json=[1, 2])
    assert r.status_code == 400 and "JSON 객체" in r.json()["detail"]


def test_AI_설정이_틀리면_503이고_받지_않는다(conn):
    pool = make_pool(database_url_for_tests())
    with app_with(pool, config_error="RAG_LLM_MODEL 이 필요함") as c:
        r = c.post(URL, json=load_request())
    pool.close()
    assert r.status_code == 503 and r.json()["error"] == "NOT_READY"
    assert rows(conn) == []


def test_DB를_쓸_수_없으면_503():
    pool = make_pool("postgresql://127.0.0.1:1/nothing?connect_timeout=1", timeout=0.5)
    with app_with(pool) as c:
        r = c.post(URL, json=load_request())
    pool.close()
    assert r.status_code == 503 and r.json() == {"error": "NOT_READY", "detail": "DB 를 쓸 수 없음"}


def test_런타임은_주소를_붙이고_작업자를_띄웠다가_멈춘다(conn):
    env = {"MINUTES_DATABASE_URL": database_url_for_tests(), "MINUTES_CALLBACK_ALLOWED_HOSTS": "minutes-service:8084",
           "RAG_LLM_PROVIDER": "fake"}
    app = FastAPI()
    runtime = setup(app, env)
    runtime.start()
    try:
        assert runtime.thread.is_alive()
        with TestClient(app) as c:
            assert c.post(URL, json=load_request()).status_code == 202
    finally:
        runtime.stop(timeout_s=10)
    assert not runtime.thread.is_alive()


def test_런타임은_AI_설정이_틀리면_작업자를_띄우지_않고_503(conn):
    env = {"MINUTES_DATABASE_URL": database_url_for_tests(), "RAG_LLM_PROVIDER": "nope"}
    app = FastAPI()
    runtime = setup(app, env)
    runtime.start()
    try:
        assert runtime.thread is None
        with TestClient(app) as c:
            assert c.post(URL, json=load_request()).status_code == 503
    finally:
        runtime.stop()
