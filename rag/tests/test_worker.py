"""대기열·작업자 시험 (시험 DB 필요). 임베더와 백엔드는 가짜를 쓴다."""
import copy
import json
import threading
import time
from pathlib import Path

import pytest

from _helpers import OTHER_WS, WS
from rag.callback import CallbackNotAllowed, CallbackSender
from rag.jobs import IngestRequest, claim, cleanup_finished, submit
from rag.store import DocumentMeta, delete_document, delete_workspace, get_status, register
from rag.worker import Worker
from upload_fakes import ole_bytes

CB = "http://backend.test:8080/internal/rag/callback"
MINUTES = json.loads((Path(__file__).parent / "fixtures" / "minutes_basic.json").read_text(encoding="utf-8"))
MINUTES_PIECES = 4                                   # 시험 회의록에서 넣을 항목 수 (test_minutes_loader 와 같음)


class FakeBackend:
    def __init__(self, *results):
        self.calls, self.results = [], list(results)

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return self.results.pop(0) if self.results else (True, False, 200)

    @property
    def bodies(self):
        return [c[1] for c in self.calls]


class HookEmbedder:
    """가짜 임베더를 감싸서, 임베딩할 때 실패를 내거나 끼어들 일을 한다."""

    def __init__(self, inner, fail_times=0, on_embed=None):
        self.inner, self.fail_times, self.on_embed = inner, fail_times, on_embed
        self.model_id, self.dim, self.max_tokens = inner.model_id, inner.dim, inner.max_tokens

    def passage_tokens(self, text):
        return self.inner.passage_tokens(text)

    def embed_passages(self, texts):
        if self.on_embed:
            self.on_embed()
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("일시 실패 흉내")
        return self.inner.embed_passages(texts)


def request(doc_id=1, version="v1", ws=WS, source="minutes", minutes=None, request_id="req-1", cb=CB,
            file_url="https://s3.test/presigned"):
    doc = DocumentMeta(doc_id=doc_id, doc_version=version, workspace_id=ws, source_type=source, title="주간 회의")
    if source == "minutes":
        return IngestRequest(doc, cb, minutes=copy.deepcopy(minutes or MINUTES), request_id=request_id)
    return IngestRequest(doc, cb, file_url=file_url, request_id=request_id)


def make_worker(conn, embedder, backend, sleeps=None, upload=None):
    sender = CallbackSender(sender=backend, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return Worker(conn, embedder, sender, upload=upload)


def job_rows(conn):
    return conn.execute("SELECT id, doc_id, doc_version, status, attempts, last_error, payload, request_id "
                        "FROM rag_jobs ORDER BY id").fetchall()


def chunk_count(conn, doc_id):
    return conn.execute("SELECT count(*) FROM rag_chunks WHERE doc_id = %s", (doc_id,)).fetchone()[0]


# ---- 접수 ----

def test_접수하면_작업_한_줄과_payload(conn):
    assert submit(conn, request()).enqueue
    (_, doc_id, version, status, attempts, _, payload, request_id), = job_rows(conn)
    assert (doc_id, version, status, attempts, request_id) == (1, "v1", "queued", 0, "req-1")
    assert payload["sourceType"] == "minutes" and payload["callbackUrl"] == CB
    assert payload["minutes"]["schema_version"] == "0.1"


def test_같은_버전을_두_번_접수해도_작업은_하나(conn):
    submit(conn, request())
    assert not submit(conn, request()).enqueue
    assert len(job_rows(conn)) == 1


def test_새_버전을_접수하면_꺼내지_않은_옛_작업은_지움(conn):
    submit(conn, request(version="v1"))
    submit(conn, request(version="v2"))
    assert [(r[2], r[3]) for r in job_rows(conn)] == [("v2", "queued")]


def test_허용하지_않은_콜백_주소는_접수하지_않음(conn):
    with pytest.raises(CallbackNotAllowed):
        submit(conn, request(cb="http://169.254.169.254/latest/meta-data"), allowed_hosts={"backend.test:8080"})
    assert get_status(conn, 1) is None and job_rows(conn) == []


# ---- 처리 ----

def test_처리하면_ready_콜백과_추적_ID가_가고_payload를_비움(conn, fake_embedder):
    backend = FakeBackend()
    submit(conn, request())
    worker = make_worker(conn, fake_embedder, backend)
    assert worker.process_one()
    assert get_status(conn, 1)["status"] == "ready" and chunk_count(conn, 1) == MINUTES_PIECES
    (url, body, headers), = backend.calls
    assert url == CB and headers["X-Request-Id"] == "req-1"
    assert body == {"docId": 1, "docVersion": "v1", "status": "ready", "errorCode": None, "chunkCount": MINUTES_PIECES}
    (_, _, _, status, attempts, last_error, payload, _), = job_rows(conn)
    assert (status, attempts, last_error, payload) == ("done", 1, None, {})
    assert not worker.process_one()                  # 더 꺼낼 것이 없음


def test_처리하는_동안_문서는_processing(conn, other_conn, fake_embedder):
    seen = []
    emb = HookEmbedder(fake_embedder, on_embed=lambda: seen.append(get_status(other_conn, 1)["status"]))
    submit(conn, request())
    make_worker(conn, emb, FakeBackend()).process_one()
    assert seen == ["processing"]


def test_넣을_항목이_없으면_재시도_없이_failed_콜백(conn, fake_embedder):
    backend = FakeBackend()
    empty = {"schema_version": "0.1", "summary": [{"text": " ", "evidence": [{"start": 1, "end": 2}]}]}
    submit(conn, request(minutes=empty))
    make_worker(conn, fake_embedder, backend).process_one()
    assert backend.bodies == [{"docId": 1, "docVersion": "v1", "status": "failed", "errorCode": "no_text",
                               "chunkCount": 0}]
    status = get_status(conn, 1)
    assert (status["status"], status["errorCode"]) == ("failed", "no_text")
    (_, _, _, job_status, attempts, last_error, payload, _), = job_rows(conn)
    assert (job_status, attempts, payload) == ("failed", 1, {}) and last_error.startswith("no_text")


def test_엑셀은_받아서_행마다_저장하고_ready(conn, fake_embedder, storage):
    backend = FakeBackend()
    storage.files["/ws/1/edge.xlsx"] = (Path(__file__).parent / "fixtures" / "upload_edge.xlsx").read_bytes()
    submit(conn, request(source="upload", version="sha256:xlsx", file_url=storage.url("/ws/1/edge.xlsx")))
    make_worker(conn, fake_embedder, backend, upload=storage.settings()).process_one()
    assert storage.hits == ["/ws/1/edge.xlsx"]
    assert backend.bodies[0]["status"] == "ready" and backend.bodies[0]["chunkCount"] == 27     # 1분기 예산 4행 + 참가 회사 23행
    labels = [r[0] for r in conn.execute("SELECT location->>'label' FROM rag_chunks WHERE doc_id = 1 ORDER BY chunk_index")]
    assert labels[0] == "1분기 예산 시트 2행" and labels[-1] == "참가 회사 시트 26행"
    _, _, _, status, attempts, _, payload, _ = job_rows(conn)[0]
    assert (status, attempts, payload) == ("done", 1, {})     # 서명 주소가 든 요청은 지운다


def test_PDF는_받아서_쪽_번호로_저장하고_ready(conn, fake_embedder, storage):
    backend = FakeBackend()
    storage.files["/ws/1/report.pdf"] = (Path(__file__).parent / "fixtures" / "upload_report.pdf").read_bytes()
    submit(conn, request(source="upload", version="sha256:abc", file_url=storage.url("/ws/1/report.pdf")))
    make_worker(conn, fake_embedder, backend, upload=storage.settings()).process_one()
    assert backend.bodies[0]["status"] == "ready" and backend.bodies[0]["chunkCount"] >= 3
    pages = {r[0] for r in conn.execute("SELECT location->>'label' FROM rag_chunks WHERE doc_id = 1")}
    assert pages == {"1쪽", "2쪽", "4쪽"}                       # 3쪽은 빈 쪽
    assert job_rows(conn)[0][6] == {}                           # 서명 주소가 든 요청은 지운다


def test_조각_토큰_합계가_상한을_넘으면_임베딩_전에_too_large(conn, fake_embedder, storage):
    backend, embedded = FakeBackend(), []
    storage.files["/ws/1/report.pdf"] = (Path(__file__).parent / "fixtures" / "upload_report.pdf").read_bytes()
    submit(conn, request(source="upload", version="sha256:big", file_url=storage.url("/ws/1/report.pdf")))
    sender = CallbackSender(sender=backend, sleep=lambda s: None)
    worker = Worker(conn, HookEmbedder(fake_embedder, on_embed=lambda: embedded.append(1)), sender,
                    upload=storage.settings(), max_doc_tokens=50)
    worker.process_one()
    assert embedded == []                                       # 임베딩하지 않고 막음
    body = backend.bodies[0]
    assert (body["status"], body["errorCode"]) == ("failed", "too_large")
    assert get_status(conn, 1)["status"] == "failed"


def test_토큰_상한은_환경변수로_바꾼다(fake_embedder):
    from rag.config import MAX_DOC_TOKENS
    from rag.worker import from_env
    assert from_env({}, embedder=fake_embedder).max_doc_tokens == MAX_DOC_TOKENS
    assert from_env({"RAG_MAX_DOC_TOKENS": "1000"}, embedder=fake_embedder).max_doc_tokens == 1000


def test_워드는_받아서_제목마다_저장하고_ready(conn, fake_embedder, storage):
    backend = FakeBackend()
    storage.files["/ws/1/edge.docx"] = (Path(__file__).parent / "fixtures" / "upload_edge.docx").read_bytes()
    submit(conn, request(source="upload", version="sha256:docx", file_url=storage.url("/ws/1/edge.docx")))
    make_worker(conn, fake_embedder, backend, upload=storage.settings()).process_one()
    assert backend.bodies[0]["status"] == "ready" and backend.bodies[0]["chunkCount"] == 4
    labels = [r[0] for r in conn.execute("SELECT location->>'label' FROM rag_chunks WHERE doc_id = 1 ORDER BY chunk_index")]
    assert labels == ["문서 처음", "1. 회의실 예약 규칙", "1.1 예약 시간", "2. 정리 규칙"]


@pytest.mark.parametrize("path, code", [("/expired", "url_expired"), ("/error", "download_failed"),
                                        ("/ws/1/old.doc", "unsupported_format"), ("/ws/1/locked.docx", "encrypted")])
def test_업로드_실패는_다시_시도하지_않고_바로_알린다(conn, fake_embedder, storage, path, code):
    """받기 실패·만료는 RAG 명세대로 백엔드가 다시 호출한다"""
    backend = FakeBackend()
    storage.files["/ws/1/old.doc"] = ole_bytes(encrypted=False)
    storage.files["/ws/1/locked.docx"] = ole_bytes(encrypted=True)
    submit(conn, request(source="upload", file_url=storage.url(path)))
    make_worker(conn, fake_embedder, backend, upload=storage.settings()).process_one()
    assert backend.bodies[0]["status"] == "failed" and backend.bodies[0]["errorCode"] == code
    assert job_rows(conn)[0][3:5] == ("failed", 1)


def test_일시_실패는_뒤로_미뤄_다시_처리(conn, fake_embedder):
    backend = FakeBackend()
    submit(conn, request())
    worker = make_worker(conn, HookEmbedder(fake_embedder, fail_times=1), backend)
    assert worker.process_one()
    (_, _, _, status, attempts, last_error, _, _), = job_rows(conn)
    assert (status, attempts, last_error) == ("queued", 1, "RuntimeError") and backend.calls == []
    assert not worker.process_one()                  # run_after 가 아직 안 됨
    conn.execute("UPDATE rag_jobs SET run_after = now()")
    assert worker.process_one()
    assert backend.bodies[0]["status"] == "ready" and job_rows(conn)[0][4] == 2


def test_일시_실패를_세_번_하면_internal_error(conn, fake_embedder):
    backend = FakeBackend()
    submit(conn, request())
    worker = make_worker(conn, HookEmbedder(fake_embedder, fail_times=3), backend)
    for _ in range(3):
        conn.execute("UPDATE rag_jobs SET run_after = now()")
        worker.process_one()
    assert backend.bodies == [{"docId": 1, "docVersion": "v1", "status": "failed", "errorCode": "internal_error",
                               "chunkCount": 0}]
    assert job_rows(conn)[0][3:5] == ("failed", 3)


# ---- 결과 버림 ----

def test_꺼낼_때_이미_새_버전이면_임베딩하지_않고_버림(conn, fake_embedder):
    calls = []
    submit(conn, request(version="v1"))
    register(conn, DocumentMeta(1, "v2", WS, "minutes", "주간 회의"))   # 작업 없이 버전만 바뀐 상황
    backend = FakeBackend()
    make_worker(conn, HookEmbedder(fake_embedder, on_embed=lambda: calls.append(1)), backend).process_one()
    assert calls == [] and backend.calls == []
    assert job_rows(conn)[0][3] == "done" and job_rows(conn)[0][5] == "stale"


def test_처리_도중_지워지면_콜백_없이_버림(conn, other_conn, fake_embedder):
    backend = FakeBackend()
    submit(conn, request())
    emb = HookEmbedder(fake_embedder, on_embed=lambda: delete_document(other_conn, 1))
    make_worker(conn, emb, backend).process_one()
    assert backend.calls == [] and get_status(conn, 1) is None and chunk_count(conn, 1) == 0


# ---- 동시성·회수 ----

def test_작업자_둘이_같은_작업을_잡지_않음(conn, other_conn):
    submit(conn, request(doc_id=1))
    submit(conn, request(doc_id=2))
    with other_conn.transaction():
        first = claim(other_conn)                    # 커밋 전이라 이 줄은 잠겨 있다
        second = claim(conn)
        assert first.doc_id == 1 and second.doc_id == 2
        assert claim(conn) is None


def test_오래_멈춘_running_작업은_다시_잡음(conn):
    submit(conn, request())
    first = claim(conn)
    assert claim(conn) is None                       # 방금 잡은 것은 다시 잡지 않음
    conn.execute("UPDATE rag_jobs SET started_at = now() - interval '11 minutes'")
    again = claim(conn)
    assert (again.id, again.attempts) == (first.id, 2)


def test_시도_횟수를_넘으면_failed(conn, fake_embedder):
    backend = FakeBackend()
    submit(conn, request())
    conn.execute("UPDATE rag_jobs SET status = 'running', attempts = 3, started_at = now() - interval '11 minutes'")
    make_worker(conn, fake_embedder, backend).process_one()
    assert backend.bodies[0]["errorCode"] == "internal_error"
    assert "시도 횟수 초과" in job_rows(conn)[0][5]


# ---- 콜백 실패 ----

def test_콜백이_5xx_면_재시도하고_결과는_그대로(conn, fake_embedder):
    backend = FakeBackend((False, True, 503), (True, False, 200))
    sleeps = []
    submit(conn, request())
    make_worker(conn, fake_embedder, backend, sleeps).process_one()
    assert len(backend.calls) == 2 and sleeps == [1.0]
    assert job_rows(conn)[0][3:6] == ("done", 1, None)


def test_콜백이_끝내_실패해도_문서는_ready_로_남음(conn, fake_embedder):
    backend = FakeBackend((False, False, 400))
    submit(conn, request())
    make_worker(conn, fake_embedder, backend).process_one()
    assert get_status(conn, 1)["status"] == "ready"
    assert job_rows(conn)[0][3:6] == ("done", 1, "callback_failed")


# ---- 정리 ----

def test_문서를_지우면_그_문서의_작업도_지움(conn):
    submit(conn, request(doc_id=1))
    submit(conn, request(doc_id=2))
    delete_document(conn, 1)
    assert [r[1] for r in job_rows(conn)] == [2]


def test_워크스페이스를_지우면_그_워크스페이스의_작업만_지움(conn):
    submit(conn, request(doc_id=1, ws=WS))
    submit(conn, request(doc_id=2, ws=OTHER_WS))
    delete_workspace(conn, WS)
    assert [r[1] for r in job_rows(conn)] == [2]


def test_끝난_작업은_7일이_지나면_지움(conn):
    for doc_id in (1, 2, 3, 4):
        submit(conn, request(doc_id=doc_id))
    conn.execute("UPDATE rag_jobs SET status = 'done', updated_at = now() - interval '8 days' WHERE doc_id = 1")
    conn.execute("UPDATE rag_jobs SET status = 'failed', updated_at = now() - interval '8 days' WHERE doc_id = 2")
    conn.execute("UPDATE rag_jobs SET status = 'done', updated_at = now() - interval '6 days' WHERE doc_id = 3")
    conn.execute("UPDATE rag_jobs SET updated_at = now() - interval '8 days' WHERE doc_id = 4")   # 아직 queued
    assert cleanup_finished(conn) == 2
    assert [r[1] for r in job_rows(conn)] == [3, 4]


# ---- DB 접속 ----

def run_in_thread(worker, poll_s=0.01):
    stop = threading.Event()
    t = threading.Thread(target=worker.run, args=(stop,), kwargs={"poll_s": poll_s}, daemon=True)
    t.start()
    return t, stop


def wait_until(cond, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while not cond():
        assert time.monotonic() < deadline, "시간 안에 조건이 맞지 않음"
        time.sleep(0.02)


def test_DB에_접속할_수_없어도_작업자는_멈추지_않고_다시_시도(fake_embedder):
    import psycopg

    calls = []

    def refuse():
        calls.append(1)
        raise psycopg.OperationalError("접속 실패 흉내")

    w = Worker(None, fake_embedder, CallbackSender(sender=FakeBackend()), connect=refuse)
    t, stop = run_in_thread(w)
    wait_until(lambda: len(calls) >= 3)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive() and w.conn is None


def test_연결이_끊기면_다시_접속해_대기_작업을_처리(conn, fake_embedder):
    from rag.db import connect

    def connect_db():
        return connect(env="RAG_TEST_DATABASE_URL")

    first = connect_db()
    conn.execute("SELECT pg_terminate_backend(%s)", (first.info.backend_pid,))   # 작업자 연결을 DB 쪽에서 끊음
    submit(conn, request())
    backend = FakeBackend()
    w = Worker(first, fake_embedder, CallbackSender(sender=backend, sleep=lambda s: None), connect=connect_db)
    t, stop = run_in_thread(w)
    try:
        wait_until(lambda: job_rows(conn)[0][3] == "done")
    finally:
        stop.set()
        t.join(timeout=5)
        w.close()
    assert backend.bodies[0]["status"] == "ready"
    assert chunk_count(conn, 1) == MINUTES_PIECES
