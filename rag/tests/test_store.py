"""접수·저장·삭제 시험 (시험 DB 필요)."""
import pytest

import threading

from _helpers import OTHER_WS, WS, chunk_rows, chunks_of, doc, put
from rag.migrate import migrate
from rag.search import search
from rag.store import (RegisterResult, StaleJob, WorkspaceMismatch, delete_document, delete_workspace,
                       get_status, mark_failed, register, store)

QUEUED_NEW = RegisterResult("queued", True)


class PausingVectors(list):
    """store() 가 처음 돌기 시작할 때(이미 지우기를 마친 뒤) 멈춘다. 교체 도중 상태를 보기 위한 것."""

    def __init__(self, vecs, reached, go):
        super().__init__(vecs)
        self.reached, self.go = reached, go

    def __iter__(self):
        self.reached.set()
        self.go.wait(timeout=5)
        return super().__iter__()


def test_마이그레이션은_두_번_돌려도_그대로(conn):
    assert migrate(conn) == []


def test_새_문서는_queued(conn):
    assert register(conn, doc()) == QUEUED_NEW
    assert get_status(conn, 1) == {"docId": 1, "docVersion": "v1", "status": "queued",
                                   "errorCode": None, "chunkCount": 0}


def test_처리_전에_같은_버전이_또_오면_작업을_또_넣지_않음(conn):
    register(conn, doc())
    assert register(conn, doc()) == RegisterResult("queued", False)


def test_새_문서를_동시에_두_번_접수해도_오류_없이_하나(conn, other_conn):
    """다른 연결이 같은 docId 를 넣고 아직 커밋하지 않은 사이에 접수한다.
    고치기 전 코드는 SELECT 에서 행을 못 보고 INSERT 하다가 기본키 중복 오류가 났다."""
    result, errors = [], []

    def late():
        try:
            result.append(register(conn, doc()))      # 다른 연결의 커밋을 기다린다
        except Exception as e:                       # 시험 실패로 보고하려고 모아 둔다
            errors.append(e)

    t = threading.Thread(target=late)
    with other_conn.transaction():                   # 블록을 나가면 커밋 (단언이 실패하면 롤백)
        other_conn.execute("INSERT INTO rag_documents (doc_id, doc_version, workspace_id, source_type, title, status) "
                           "VALUES (1, 'v1', %s, 'minutes', '주간 회의', 'queued')", (WS,))
        t.start()
        t.join(timeout=1)
        assert t.is_alive()                          # 잠금 때문에 기다리는 중이어야 한다
    t.join(timeout=5)
    assert not t.is_alive() and errors == []
    assert result == [RegisterResult("queued", False)]
    assert conn.execute("SELECT count(*) FROM rag_documents").fetchone()[0] == 1


def test_저장하면_ready(conn, fake_embedder):
    assert put(conn, fake_embedder, doc(), "가", "나") == 2
    assert get_status(conn, 1)["status"] == "ready"
    assert get_status(conn, 1)["chunkCount"] == 2
    assert [r[2] for r in chunk_rows(conn, 1)] == ["가", "나"]


def test_같은_버전이_두_번_와도_결과는_하나(conn, fake_embedder):
    put(conn, fake_embedder, doc(), "가", "나")
    assert register(conn, doc()) == RegisterResult("ready", False)   # 다시 처리하지 않음
    put(conn, fake_embedder, doc(), "가", "나")                # 작업자가 같은 버전을 다시 처리해도
    assert len(chunk_rows(conn, 1)) == 2
    assert conn.execute("SELECT count(*) FROM rag_documents").fetchone()[0] == 1


def test_새_버전은_교체되고_교체_전까지_옛_버전이_남음(conn, fake_embedder):
    put(conn, fake_embedder, doc(version="v1"), "옛 글")
    assert register(conn, doc(version="v2")) == QUEUED_NEW
    assert chunk_rows(conn, 1) == [("v1", 0, "옛 글")]       # 처리 중에도 옛 버전은 그대로
    put(conn, fake_embedder, doc(version="v2"), "새 글 하나", "새 글 둘")
    assert [r[0] for r in chunk_rows(conn, 1)] == ["v2", "v2"]
    assert get_status(conn, 1)["docVersion"] == "v2"


def test_넣다가_실패하면_지운_것까지_롤백되어_옛_버전이_남음(conn, fake_embedder):
    import psycopg

    put(conn, fake_embedder, doc(version="v1"), "옛 글")
    register(conn, doc(version="v2"))
    cs = chunks_of("새 글 하나", "새 글 둘")
    vecs = fake_embedder.embed_passages([c.embed_text for c in cs])
    vecs[1] = vecs[1][:383]                          # 차원이 틀린 벡터 -> 지우기 뒤 넣기에서 DB 가 거부
    with pytest.raises(psycopg.Error):
        store(conn, 1, "v2", cs, vecs, fake_embedder.model_id)
    assert chunk_rows(conn, 1) == [("v1", 0, "옛 글")]
    status = get_status(conn, 1)
    assert (status["status"], status["chunkCount"]) == ("queued", 1)


def test_교체_도중에도_다른_연결에서는_옛_버전이_보이고_검색됨(conn, other_conn, fake_embedder):
    put(conn, fake_embedder, doc(version="v1"), "옛 글")
    register(conn, doc(version="v2"))
    reached, go = threading.Event(), threading.Event()
    cs = chunks_of("새 글")
    vecs = PausingVectors(fake_embedder.embed_passages(["새 글"]), reached, go)
    errors = []

    def work():
        try:
            store(conn, 1, "v2", cs, vecs, fake_embedder.model_id)
        except Exception as e:                       # 시험 실패로 보고하려고 모아 둔다
            errors.append(e)

    t = threading.Thread(target=work)
    t.start()
    try:
        assert reached.wait(timeout=5)               # 지우기는 끝났고 넣기 직전에 멈춤
        assert chunk_rows(other_conn, 1) == [("v1", 0, "옛 글")]
        assert search(other_conn, fake_embedder, WS, [1], "옛 글")[0].content == "옛 글"
    finally:
        go.set()
        t.join(timeout=5)
    assert errors == []
    assert chunk_rows(other_conn, 1) == [("v2", 0, "새 글")]


def test_처리_도중_새_버전이_오면_옛_작업은_버려짐(conn, fake_embedder):
    register(conn, doc(version="v1"))
    register(conn, doc(version="v2"))
    cs = chunks_of("옛 글")
    with pytest.raises(StaleJob):
        store(conn, 1, "v1", cs, fake_embedder.embed_passages(["옛 글"]), fake_embedder.model_id)
    assert chunk_rows(conn, 1) == []
    assert get_status(conn, 1)["status"] == "queued"


def test_처리_도중_지우면_다시_넣지_않음(conn, fake_embedder):
    register(conn, doc())
    delete_document(conn, 1)
    with pytest.raises(StaleJob):
        store(conn, 1, "v1", chunks_of("가"), fake_embedder.embed_passages(["가"]), fake_embedder.model_id)
    assert get_status(conn, 1) is None
    assert chunk_rows(conn, 1) == []


def test_없는_문서를_지워도_오류_아님(conn):
    delete_document(conn, 999)


def test_문서를_지우면_조각도_지워짐(conn, fake_embedder):
    put(conn, fake_embedder, doc(), "가")
    delete_document(conn, 1)
    assert chunk_rows(conn, 1) == []


def test_워크스페이스_지우기는_그_워크스페이스만(conn, fake_embedder):
    put(conn, fake_embedder, doc(1, ws=WS), "가")
    put(conn, fake_embedder, doc(2, ws=WS), "나")
    put(conn, fake_embedder, doc(3, ws=OTHER_WS), "다")
    assert delete_workspace(conn, WS) == 2
    assert get_status(conn, 1) is None and get_status(conn, 2) is None
    assert get_status(conn, 3)["status"] == "ready"


def test_실패한_버전이_다시_오면_재시도(conn):
    register(conn, doc())
    assert mark_failed(conn, 1, "v1", "no_text")
    assert get_status(conn, 1)["errorCode"] == "no_text"
    assert register(conn, doc()) == QUEUED_NEW
    assert get_status(conn, 1)["errorCode"] is None


def test_새_버전이_실패해도_옛_버전_조각은_남음(conn, fake_embedder):
    put(conn, fake_embedder, doc(version="v1"), "옛 글")
    register(conn, doc(version="v2"))
    mark_failed(conn, 1, "v2", "internal_error")
    assert chunk_rows(conn, 1) == [("v1", 0, "옛 글")]


def test_옛_버전의_실패는_현재_상태를_바꾸지_않음(conn):
    register(conn, doc(version="v1"))
    register(conn, doc(version="v2"))
    assert not mark_failed(conn, 1, "v1", "internal_error")
    assert get_status(conn, 1)["status"] == "queued"


def test_같은_docId_가_다른_워크스페이스로_오면_거부(conn):
    register(conn, doc(ws=WS))
    with pytest.raises(WorkspaceMismatch):
        register(conn, doc(ws=OTHER_WS))


def test_조각과_벡터_수가_다르면_거부(conn, fake_embedder):
    register(conn, doc())
    with pytest.raises(ValueError):
        store(conn, 1, "v1", chunks_of("가", "나"), fake_embedder.embed_passages(["가"]), fake_embedder.model_id)
