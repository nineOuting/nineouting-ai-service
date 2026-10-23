"""회의록 초안 작업 대기열 (minutes_jobs). RAG 대기열(rag/jobs.py)과 같은 방식이다.

  submit()   접수: 같은 request_id 가 처리 중이거나 성공 결과를 보냈으면 DUPLICATE, 실패로 끝났으면 새로 받는다
  claim()    꺼내기: FOR UPDATE SKIP LOCKED 로 하나를 잡아 running 으로. 오래 멈춘 running 도 다시 잡는다
  finish()   끝내기: done / failed 로 바꾸고 payload(요청 본문)를 비운다
  retry_later()  일시 실패: queued 로 되돌리고 run_after 를 뒤로 미룬다
  cleanup_finished()  끝난 작업 줄을 보관 기간이 지나면 지운다
"""
from dataclasses import dataclass

from psycopg.types.json import Jsonb

STUCK_AFTER_S = 600              # running 으로 이만큼 멈춰 있으면 작업자가 죽은 것으로 보고 다시 잡는다
KEEP_DAYS = 7                    # 끝난 작업 줄 보관 기간 (중복 판단에도 쓰인다)
ACCEPTED, DUPLICATE = "ACCEPTED", "DUPLICATE"


@dataclass(frozen=True)
class Job:
    id: int
    request_id: str
    minutes_id: int
    payload: dict
    callback_url: str
    trace_id: str | None
    attempts: int


# 같은 request_id 가 있으면, 실패로 끝난 경우에만 새 요청으로 덮어쓴다. 아니면 아무것도 바꾸지 않아 RETURNING 이 비고 DUPLICATE 다
_SUBMIT = """
INSERT INTO minutes_jobs (request_id, minutes_id, payload, callback_url, trace_id)
VALUES (%(request_id)s, %(minutes_id)s, %(payload)s, %(callback_url)s, %(trace_id)s)
ON CONFLICT (request_id) DO UPDATE SET
  minutes_id = EXCLUDED.minutes_id, payload = EXCLUDED.payload, callback_url = EXCLUDED.callback_url,
  trace_id = EXCLUDED.trace_id, status = 'queued', result_status = NULL, error_code = NULL, attempts = 0,
  last_error = NULL, run_after = now(), started_at = NULL, updated_at = now()
WHERE minutes_jobs.status = 'failed' OR (minutes_jobs.status = 'done' AND minutes_jobs.result_status = 'FAILED')
RETURNING id
"""


def submit(conn, body: dict, trace_id: str | None = None) -> str:
    """검사를 통과한 요청 본문을 넣는다. ACCEPTED 또는 DUPLICATE."""
    row = conn.execute(_SUBMIT, {"request_id": body["requestId"], "minutes_id": body["minutesId"],
                                 "payload": Jsonb(body), "callback_url": body["callbackUrl"],
                                 "trace_id": trace_id}).fetchone()
    return ACCEPTED if row else DUPLICATE


_CLAIM = """
UPDATE minutes_jobs SET status = 'running', attempts = attempts + 1, started_at = now(), updated_at = now()
WHERE id = (SELECT id FROM minutes_jobs
            WHERE (status = 'queued' AND run_after <= now())
               OR (status = 'running' AND started_at < now() - make_interval(secs => %s))
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
RETURNING id, request_id, minutes_id, payload, callback_url, trace_id, attempts
"""


def claim(conn, stuck_after_s=STUCK_AFTER_S) -> Job | None:
    row = conn.execute(_CLAIM, (stuck_after_s,)).fetchone()
    return Job(*row) if row else None


def finish(conn, job_id: int, status: str, result_status: str | None = None, error_code: str | None = None,
           last_error: str | None = None) -> None:
    """status: done(결과 콜백을 보냄) / failed(못 보냄). result_status: 보낸(보내려던) 결과 SUCCEEDED / FAILED."""
    conn.execute("UPDATE minutes_jobs SET status = %s, result_status = %s, error_code = %s, last_error = %s, "
                 "payload = '{}'::jsonb, updated_at = now() WHERE id = %s",
                 (status, result_status, error_code, last_error, job_id))


def retry_later(conn, job_id: int, delay_s: float, last_error: str) -> None:
    conn.execute("UPDATE minutes_jobs SET status = 'queued', run_after = now() + make_interval(secs => %s), "
                 "last_error = %s, started_at = NULL, updated_at = now() WHERE id = %s",
                 (delay_s, last_error, job_id))


def cleanup_finished(conn, keep_days=KEEP_DAYS) -> int:
    return conn.execute("DELETE FROM minutes_jobs WHERE status IN ('done', 'failed') "
                        "AND updated_at < now() - make_interval(days => %s)", (keep_days,)).rowcount
