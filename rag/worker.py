"""문서 임베딩 작업자.

실행: .venv/bin/python -m rag.worker        (따로 띄우기)
      start_in_thread(...)                  (API 프로세스 안의 스레드로, SCRUM-70 에서 씀)

작업 하나의 흐름
  꺼내기 → (처리 전 확인) → 로더 → 나누기 → 임베딩 → store() → 콜백 → 끝내기(payload 비움)

실패 처리
  영구 실패   IngestError (no_text, 형식 오류, 업로드 로더 미구현 등) → 재시도 없이 failed 콜백
  일시 실패   그 밖의 예외 → 최대 MAX_ATTEMPTS 번까지 RETRY_DELAYS_S 만큼 미뤄 다시 시도, 다 쓰면 internal_error
  결과 버림   문서가 지워졌거나 더 새 버전이 접수됨 → 콜백 없이 끝냄 (새 버전은 새 버전의 콜백이 간다)

로그에는 작업·문서 번호, 버전, 상태, 오류 코드, 요청 추적 ID, 조각 수, 걸린 시간만 남긴다.
"""
import logging
import os
import signal
import threading
import time

from .callback import CallbackSender, allowed_hosts_from_env
from .chunking import split_pieces
from .errors import INTERNAL_ERROR, UNSUPPORTED_FORMAT, IngestError
from .jobs import KEEP_DAYS, claim, cleanup_finished, finish, retry_later
from .loaders.minutes import load_minutes
from .store import StaleJob, is_current, mark_failed, mark_processing, store

log = logging.getLogger("rag.worker")

MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (30, 120)       # 1번째 실패 뒤 30초, 2번째 실패 뒤 2분
POLL_S = 1.0                     # 할 일이 없을 때 다시 묻는 간격
CLEANUP_EVERY_S = 3600           # 끝난 작업 줄 정리 주기
UPLOAD_NOT_READY = "업로드 로더 미구현"


class Worker:
    def __init__(self, conn, embedder, callback, max_attempts=MAX_ATTEMPTS, retry_delays_s=RETRY_DELAYS_S,
                 connect=None):
        """connect 를 주면 conn 이 없거나 끊겼을 때 run() 이 그것으로 다시 접속한다 (conn=None 으로 시작해도 됨)."""
        self.conn = conn
        self.embedder = embedder
        self.callback = callback
        self.max_attempts = max_attempts
        self.retry_delays_s = tuple(retry_delays_s)
        self._connect = connect

    def process_one(self) -> bool:
        """작업 하나를 처리했으면 True, 꺼낼 것이 없으면 False."""
        job = claim(self.conn)
        if job is None:
            return False
        started = time.monotonic()
        tag = f"job={job.id} doc={job.doc_id} version={job.doc_version} attempt={job.attempts} request_id={job.request_id}"

        if job.attempts > self.max_attempts:          # 멈춘 작업을 다시 잡다 보면 횟수를 넘을 수 있다
            self._fail(job, INTERNAL_ERROR, "시도 횟수 초과", tag)
            return True
        if not is_current(self.conn, job.doc_id, job.doc_version):
            finish(self.conn, job.id, "done", "stale")
            log.info("결과 버림(처리 전) %s", tag)
            return True

        try:
            mark_processing(self.conn, job.doc_id, job.doc_version)
            chunks, vectors = self._prepare(job)
            count = store(self.conn, job.doc_id, job.doc_version, chunks, vectors, self.embedder.model_id)
        except StaleJob:
            finish(self.conn, job.id, "done", "stale")
            log.info("결과 버림(저장 시) %s", tag)
        except IngestError as e:
            self._fail(job, e.code, e.detail, tag)
        except Exception as e:                        # 일시 실패로 본다
            if job.attempts < self.max_attempts:
                delay = self.retry_delays_s[min(job.attempts, len(self.retry_delays_s)) - 1]
                retry_later(self.conn, job.id, delay, type(e).__name__)
                log.warning("일시 실패, %s초 뒤 다시 %s error=%s", delay, tag, type(e).__name__)
            else:
                self._fail(job, INTERNAL_ERROR, type(e).__name__, tag)
        else:
            sent = self._notify(job, "ready", None, count)
            finish(self.conn, job.id, "done", None if sent else "callback_failed")
            log.info("처리 완료 %s chunks=%d 걸린시간=%.1fs", tag, count, time.monotonic() - started)
        return True

    def _prepare(self, job):
        source = job.payload.get("sourceType")
        if source == "minutes":
            pieces = load_minutes(job.payload.get("minutes")).pieces
        elif source == "upload":
            raise IngestError(UNSUPPORTED_FORMAT, UPLOAD_NOT_READY)
        else:
            raise IngestError(INTERNAL_ERROR, "알 수 없는 sourceType")
        chunks = split_pieces(pieces, self.embedder.passage_tokens, self.embedder.max_tokens)
        vectors = self.embedder.embed_passages([c.embed_text for c in chunks])
        return chunks, vectors

    def _fail(self, job, code, detail, tag):
        last_error = f"{code}: {detail}" if detail else code
        if not mark_failed(self.conn, job.doc_id, job.doc_version, code):
            finish(self.conn, job.id, "done", "stale")    # 그 사이 지워졌거나 새 버전이 왔다
            log.info("결과 버림(실패 기록 시) %s", tag)
            return
        sent = self._notify(job, "failed", code, 0)
        finish(self.conn, job.id, "failed", last_error if sent else f"{last_error} / callback_failed")
        log.warning("처리 실패 %s code=%s", tag, code)

    def _notify(self, job, status, error_code, chunk_count):
        body = {"docId": job.doc_id, "docVersion": job.doc_version, "status": status,
                "errorCode": error_code, "chunkCount": chunk_count}
        return self.callback.send(job.payload.get("callbackUrl"), body, job.request_id)

    def run(self, stop: threading.Event, poll_s=POLL_S, cleanup_every_s=CLEANUP_EVERY_S):
        """stop 이 켜질 때까지 반복한다. 처리 중인 작업은 끝까지 마친 뒤 멈춘다."""
        next_cleanup = 0.0
        while not stop.is_set():
            try:
                self._ensure_conn()
                if time.monotonic() >= next_cleanup:
                    removed = cleanup_finished(self.conn, KEEP_DAYS)
                    if removed:
                        log.info("끝난 작업 정리 %d줄", removed)
                    next_cleanup = time.monotonic() + cleanup_every_s
                if not self.process_one():
                    stop.wait(poll_s)
            except Exception as e:                    # DB 접속 실패·연결 끊김 등. 반복은 멈추지 않는다
                log.error("작업자 오류 error=%s", type(e).__name__)
                stop.wait(poll_s * 5)

    def _ensure_conn(self):
        """연결이 없거나 끊겼으면 다시 접속한다. connect 가 없으면 지금 연결을 그대로 쓴다."""
        if self._connect is None:
            return
        if self.conn is not None and not (self.conn.closed or self.conn.broken):
            return
        self.close()
        self.conn = self._connect()
        log.info("작업자 DB 접속")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def from_env(env=None):
    """환경변수로 작업자를 만든다. 임베딩 모델은 여기서 한 번 불러온다.
    DB 에는 여기서 접속하지 않는다. run() 이 처음 돌 때 접속하고, 끊기면 다시 접속한다 (DB 가 늦게 떠도 서버는 뜬다)."""
    from functools import partial

    from .db import connect
    from .embedding import E5Embedder

    env = os.environ if env is None else env
    connect_db = partial(connect, url=env.get("RAG_DATABASE_URL"),
                         statement_timeout_ms=env.get("RAG_DB_STATEMENT_TIMEOUT_MS", 30000),
                         idle_tx_timeout_ms=env.get("RAG_DB_IDLE_TX_TIMEOUT_MS", 60000),
                         connect_timeout_s=env.get("RAG_DB_CONNECT_TIMEOUT_S", 10))
    embedder = E5Embedder(threads=int(env.get("RAG_TORCH_THREADS", "2")))
    allowed = allowed_hosts_from_env(env)
    if allowed is None:
        log.warning("RAG_CALLBACK_ALLOWED_HOSTS 가 비어 있어 모든 콜백 주소를 허용함 (개발용)")
    return Worker(None, embedder, CallbackSender(allowed=allowed), connect=connect_db)


def start_in_thread(worker, stop=None):
    """API 프로세스 안에서 작업자를 스레드로 띄운다. (thread, stop) 을 돌려준다."""
    stop = stop or threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), name="rag-worker", daemon=True)
    thread.start()
    return thread, stop


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = from_env()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("작업자 시작")
    worker.run(stop)
    log.info("작업자 멈춤")


if __name__ == "__main__":
    main()
