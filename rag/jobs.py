"""작업 대기열 (rag_jobs).

  submit()   접수: register() 와 작업 넣기를 한 트랜잭션으로 (사이에 죽어도 "queued 인데 작업 없음"이 안 남게)
  claim()    꺼내기: FOR UPDATE SKIP LOCKED 로 하나를 잡아 running 으로. 오래 멈춘 running 도 다시 잡는다
  finish()   끝내기: done / failed 로 바꾸고 payload(문서 내용)를 비운다
  retry_later()  일시 실패: queued 로 되돌리고 run_after 를 뒤로 미룬다
  cleanup_finished()  끝난 작업 줄을 보관 기간이 지나면 지운다
"""
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .callback import check_callback_url
from .store import DocumentMeta, RegisterResult, register

STUCK_AFTER_S = 600              # running 으로 이만큼 멈춰 있으면 작업자가 죽은 것으로 보고 다시 잡는다
KEEP_DAYS = 7                    # 끝난 작업 줄 보관 기간


@dataclass(frozen=True)
class IngestRequest:
    doc: DocumentMeta
    callback_url: str
    minutes: dict | None = None  # sourceType=minutes
    file_url: str | None = None  # sourceType=upload
    request_id: str | None = None


@dataclass(frozen=True)
class Job:
    id: int
    doc_id: int
    doc_version: str
    workspace_id: int
    payload: dict
    attempts: int                # 이번 시도를 포함한 횟수
    request_id: str | None


def submit(conn, req: IngestRequest, allowed_hosts=None) -> RegisterResult:
    """접수. 콜백 주소가 허용되지 않으면 CallbackNotAllowed (DB 는 건드리지 않는다)."""
    check_callback_url(req.callback_url, allowed_hosts)
    payload = {"sourceType": req.doc.source_type, "callbackUrl": req.callback_url}
    if req.minutes is not None:
        payload["minutes"] = req.minutes
    if req.file_url is not None:
        payload["fileUrl"] = req.file_url
    with conn.transaction():
        result = register(conn, req.doc)
        if result.enqueue:
            # 아직 꺼내지 않은 옛 작업은 어차피 버려지므로 지운다
            conn.execute("DELETE FROM rag_jobs WHERE doc_id = %s AND status = 'queued'", (req.doc.doc_id,))
            conn.execute("INSERT INTO rag_jobs (doc_id, doc_version, workspace_id, payload, request_id) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (req.doc.doc_id, req.doc.doc_version, req.doc.workspace_id, Jsonb(payload), req.request_id))
    return result


_CLAIM = """
UPDATE rag_jobs SET status = 'running', attempts = attempts + 1, started_at = now(), updated_at = now()
WHERE id = (SELECT id FROM rag_jobs
            WHERE (status = 'queued' AND run_after <= now())
               OR (status = 'running' AND started_at < now() - make_interval(secs => %s))
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
RETURNING id, doc_id, doc_version, workspace_id, payload, attempts, request_id
"""


def claim(conn, stuck_after_s=STUCK_AFTER_S) -> Job | None:
    row = conn.execute(_CLAIM, (stuck_after_s,)).fetchone()
    return Job(*row) if row else None


def finish(conn, job_id: int, status: str, last_error: str | None = None) -> None:
    conn.execute("UPDATE rag_jobs SET status = %s, last_error = %s, payload = '{}'::jsonb, updated_at = now() "
                 "WHERE id = %s", (status, last_error, job_id))


def retry_later(conn, job_id: int, delay_s: float, last_error: str) -> None:
    conn.execute("UPDATE rag_jobs SET status = 'queued', run_after = now() + make_interval(secs => %s), "
                 "last_error = %s, started_at = NULL, updated_at = now() WHERE id = %s",
                 (delay_s, last_error, job_id))


def cleanup_finished(conn, keep_days=KEEP_DAYS) -> int:
    return conn.execute("DELETE FROM rag_jobs WHERE status IN ('done', 'failed') "
                        "AND updated_at < now() - make_interval(days => %s)", (keep_days,)).rowcount
