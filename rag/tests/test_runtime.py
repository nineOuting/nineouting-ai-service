"""server/main.py 에 붙는 RagRuntime 시험. DB 는 일부러 닿지 않는 주소를 쓴다 (시험 DB 불필요)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.api import PREFIX
from rag.runtime import setup

BAD_URL = "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"


def test_DB에_접속할_수_없어도_RAG를_켠_서버는_뜨고_멈춤(monkeypatch, fake_embedder):
    import rag.embedding
    monkeypatch.setattr(rag.embedding, "E5Embedder", lambda **kw: fake_embedder)   # 모델 없이
    env = {"RAG_DATABASE_URL": BAD_URL, "RAG_DB_POOL_TIMEOUT_S": "0.5", "RAG_DB_CONNECT_TIMEOUT_S": "1",
           "RAG_CALLBACK_ALLOWED_HOSTS": "backend.test:8080"}
    app = FastAPI()
    rt = setup(app, env)

    rt.start()                                        # 예전에는 여기서 작업자가 접속하다 실패해 서버가 뜨지 않았다
    try:
        assert rt.thread.is_alive()
        with TestClient(app) as c:
            r = c.get(f"{PREFIX}/documents/1")
            assert r.status_code == 503 and r.json()["error"] == "NOT_READY"
    finally:
        rt.stop(timeout_s=5)                          # 한 번도 접속하지 못한 채로 멈춰도 오류가 없어야 한다
    assert not rt.thread.is_alive()
