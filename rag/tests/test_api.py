"""내부 API 시험 (시험 DB 필요). 작은 앱에 라우터만 붙이고, 작업자는 띄우지 않는다."""
import copy
import json
import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import FastAPI                     # noqa: E402
from fastapi.testclient import TestClient      # noqa: E402

from _helpers import OTHER_WS, WS              # noqa: E402
from rag.api import make_router                # noqa: E402

CB = "http://backend.test:8080/internal/rag/callback"
MINUTES = json.loads((Path(__file__).parent / "fixtures" / "minutes_basic.json").read_text(encoding="utf-8"))
BASE = "/internal/rag"


def body(**over):
    b = {"docId": 1, "docVersion": "v1", "workspaceId": WS, "sourceType": "minutes", "title": "주간 회의",
         "minutes": copy.deepcopy(MINUTES), "callbackUrl": CB}
    b.update(over)
    return {k: v for k, v in b.items() if v is not _DROP}


_DROP = object()


def make_pool(url, timeout=5.0):
    from psycopg_pool import ConnectionPool
    return ConnectionPool(url, min_size=1, max_size=2, timeout=timeout, open=True, kwargs={"autocommit": True},
                          name="rag-test")


@pytest.fixture
def client(conn):                                # conn: 표를 비우고, DB 가 없으면 건너뛰게 한다
    pool = make_pool(os.environ["RAG_TEST_DATABASE_URL"])
    app = FastAPI()
    app.include_router(make_router(pool, allowed_callback_hosts={"backend.test:8080"}))
    with TestClient(app) as c:
        yield c
    pool.close()


def jobs(conn):
    return conn.execute("SELECT doc_id, doc_version, status, request_id FROM rag_jobs ORDER BY id").fetchall()


# ---- 문서 넣기 ----

def test_넣기는_202_accepted_이고_작업이_생김(client, conn):
    r = client.post(f"{BASE}/documents", json=body(), headers={"X-Request-Id": "req-1"})
    assert r.status_code == 202
    assert r.json() == {"docId": 1, "docVersion": "v1", "status": "accepted"}
    assert jobs(conn) == [(1, "v1", "queued", "req-1")]


def test_같은_버전을_두_번_넣어도_202_이고_작업은_하나(client, conn):
    for _ in range(2):
        assert client.post(f"{BASE}/documents", json=body()).status_code == 202
    assert len(jobs(conn)) == 1


def test_추적_ID는_200자까지만_저장(client, conn):
    client.post(f"{BASE}/documents", json=body(), headers={"X-Request-Id": "r" * 300})
    assert jobs(conn)[0][3] == "r" * 200


@pytest.mark.parametrize("over, word", [
    ({"docId": "1"}, "docId"),
    ({"docId": True}, "docId"),
    ({"docId": 0}, "docId"),
    ({"docId": _DROP}, "docId"),
    ({"workspaceId": -3}, "workspaceId"),
    ({"docVersion": ""}, "docVersion"),
    ({"docVersion": "v" * 201}, "docVersion"),
    ({"title": _DROP}, "title"),
    ({"sourceType": "upload", "fileUrl": "https://s3.test/x"}, "준비 중"),
    ({"sourceType": "pdf"}, "sourceType"),
    ({"minutes": _DROP}, "minutes"),
    ({"minutes": "회의록"}, "minutes"),
    ({"callbackUrl": _DROP}, "callbackUrl"),
    ({"callbackUrl": "http://169.254.169.254/latest/meta-data"}, "허용 목록"),
])
def test_입력이_틀리면_400_INVALID_INPUT_이고_아무것도_안_남김(client, conn, over, word):
    r = client.post(f"{BASE}/documents", json=body(**over))
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_INPUT" and word in r.json()["detail"]
    assert jobs(conn) == []
    assert "Jira" not in r.text                  # 오류에 회의록 내용이 섞이지 않음


def test_본문이_객체가_아니면_400(client):
    r = client.post(f"{BASE}/documents", json=[1, 2])
    assert r.status_code == 400 and r.json()["error"] == "INVALID_INPUT"


def test_같은_docId가_다른_워크스페이스로_오면_409(client):
    client.post(f"{BASE}/documents", json=body(workspaceId=WS))
    r = client.post(f"{BASE}/documents", json=body(workspaceId=OTHER_WS, docVersion="v2"))
    assert r.status_code == 409 and r.json()["error"] == "CONFLICT"


# ---- 상태 조회 ----

def test_상태_조회는_queued를_accepted로_보여줌(client):
    client.post(f"{BASE}/documents", json=body())
    r = client.get(f"{BASE}/documents/1")
    assert r.status_code == 200
    assert r.json() == {"docId": 1, "docVersion": "v1", "status": "accepted", "errorCode": None, "chunkCount": 0}


def test_상태_조회는_처리_결과를_그대로_보여줌(client, conn):
    client.post(f"{BASE}/documents", json=body())
    conn.execute("UPDATE rag_documents SET status = 'failed', error_code = 'no_text'")
    assert client.get(f"{BASE}/documents/1").json()["status"] == "failed"
    assert client.get(f"{BASE}/documents/1").json()["errorCode"] == "no_text"


def test_넣은_적_없는_문서는_404(client):
    r = client.get(f"{BASE}/documents/999")
    assert r.status_code == 404 and r.json()["error"] == "NOT_FOUND"


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "1.5"])
def test_주소의_ID가_정수가_아니면_400(client, bad):
    assert client.get(f"{BASE}/documents/{bad}").status_code == 400
    assert client.delete(f"{BASE}/documents/{bad}").status_code == 400
    assert client.delete(f"{BASE}/workspaces/{bad}").status_code == 400


# ---- 지우기 ----

def test_문서_지우기는_204이고_작업도_지움(client, conn):
    client.post(f"{BASE}/documents", json=body())
    r = client.delete(f"{BASE}/documents/1")
    assert r.status_code == 204 and r.content == b""
    assert client.get(f"{BASE}/documents/1").status_code == 404 and jobs(conn) == []


def test_없는_문서를_지워도_204(client):
    assert client.delete(f"{BASE}/documents/12345").status_code == 204


def test_워크스페이스_지우기는_그_워크스페이스만(client):
    client.post(f"{BASE}/documents", json=body(docId=1, workspaceId=WS))
    client.post(f"{BASE}/documents", json=body(docId=2, workspaceId=OTHER_WS))
    assert client.delete(f"{BASE}/workspaces/{WS}").status_code == 204
    assert client.get(f"{BASE}/documents/1").status_code == 404
    assert client.get(f"{BASE}/documents/2").status_code == 200


# ---- /docs 화면 ----

def test_docs_화면에_한글_제목과_본문_필드와_예시가_나옴():
    app = FastAPI()
    app.include_router(make_router(pool=None))       # 화면 설명만 보므로 DB 는 쓰지 않는다
    spec = TestClient(app).get("/openapi.json").json()
    post = spec["paths"][f"{BASE}/documents"]["post"]
    assert post["summary"] == "문서 넣기"
    content = post["requestBody"]["content"]["application/json"]
    assert {"docId", "docVersion", "workspaceId", "sourceType", "title", "minutes", "callbackUrl"} \
        <= set(content["schema"]["properties"])
    assert content["example"]["sourceType"] == "minutes"
    assert spec["paths"][f"{BASE}/documents/{{doc_id}}"]["get"]["summary"] == "상태 조회"
    assert spec["paths"][f"{BASE}/documents/{{doc_id}}"]["delete"]["summary"] == "문서 지우기"
    assert spec["paths"][f"{BASE}/workspaces/{{workspace_id}}"]["delete"]["summary"] == "워크스페이스 지우기"
    doc_id = spec["paths"][f"{BASE}/documents/{{doc_id}}"]["get"]["parameters"][0]
    assert "양의 정수" in doc_id["description"]


# ---- DB 를 쓸 수 없을 때 ----

def test_DB를_쓸_수_없으면_503_NOT_READY():
    pool = make_pool("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1", timeout=0.5)
    app = FastAPI()
    app.include_router(make_router(pool))
    with TestClient(app) as c:
        r = c.post(f"{BASE}/documents", json=body())
        assert r.status_code == 503 and r.json()["error"] == "NOT_READY"
        assert c.get(f"{BASE}/documents/1").status_code == 503
        assert c.delete(f"{BASE}/documents/1").status_code == 503
    pool.close(timeout=1)
