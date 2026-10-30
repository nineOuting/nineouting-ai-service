"""작업 대기열 시험 (시험 DB 필요). 넣기·중복·꺼내기·끝내기·다시 시도·정리."""
from minutes import jobs
from minutes.jobs import ACCEPTED, DUPLICATE, claim, cleanup_finished, finish, retry_later, submit
from minutes.migrate import migrate
from minutes_fakes import load_request


def body(request_id="r-1"):
    b = load_request()
    b["requestId"] = request_id
    return b


def row(conn, request_id="r-1"):
    return conn.execute("SELECT status, result_status, error_code, attempts, payload, trace_id, last_error "
                        "FROM minutes_jobs WHERE request_id = %s", (request_id,)).fetchone()


def test_마이그레이션은_두_번_돌려도_한_번만_적용한다(conn):
    assert migrate(conn) == []
    assert conn.execute("SELECT count(*) FROM minutes_schema_migrations WHERE version = '0001_jobs'").fetchone()[0] == 1


def test_넣으면_대기_상태로_요청_본문과_추적_번호를_남긴다(conn):
    assert submit(conn, body(), trace_id="t-9") == ACCEPTED
    status, result, _, attempts, payload, trace, _ = row(conn)
    assert (status, result, attempts, trace) == ("queued", None, 0, "t-9")
    assert payload["requestId"] == "r-1" and payload["minutesId"] == 41


def test_처리_중이거나_대기_중인_요청은_DUPLICATE(conn):
    submit(conn, body())
    assert submit(conn, body()) == DUPLICATE
    claim(conn)
    assert submit(conn, body()) == DUPLICATE
    assert conn.execute("SELECT count(*) FROM minutes_jobs").fetchone()[0] == 1


def test_성공_결과를_보낸_요청은_DUPLICATE(conn):
    submit(conn, body())
    job = claim(conn)
    finish(conn, job.id, "done", "SUCCEEDED")
    assert submit(conn, body()) == DUPLICATE
    assert row(conn)[0] == "done"


def test_실패_결과를_보냈거나_콜백을_못_보낸_요청은_새로_받는다(conn):
    for status, result in (("done", "FAILED"), ("failed", "SUCCEEDED")):
        conn.execute("TRUNCATE minutes_jobs RESTART IDENTITY")
        submit(conn, body(), trace_id="old")
        job = claim(conn)
        finish(conn, job.id, status, result, "LLM_TIMEOUT", "x")
        new = body()
        new["minutesId"] = 99
        assert submit(conn, new, trace_id="new") == ACCEPTED
        s, r, code, attempts, payload, trace, err = row(conn)
        assert (s, r, code, attempts, trace, err) == ("queued", None, None, 0, "new", None)
        assert payload["minutesId"] == 99


def test_꺼내기는_오래된_것부터_처리_중으로_바꾸고_횟수를_센다(conn):
    submit(conn, body("r-1"))
    submit(conn, body("r-2"))
    job = claim(conn)
    assert (job.request_id, job.attempts, job.minutes_id) == ("r-1", 1, 41)
    assert job.callback_url == load_request()["callbackUrl"] and job.payload["requestId"] == "r-1"
    assert row(conn, "r-1")[0] == "running"
    assert claim(conn).request_id == "r-2"
    assert claim(conn) is None


def test_동시에_꺼내도_같은_작업을_두_번_잡지_않는다(conn, other_conn):
    submit(conn, body("r-1"))
    submit(conn, body("r-2"))
    with conn.transaction():
        first = claim(conn)
        second = claim(other_conn)                  # 첫 연결이 잠근 줄은 건너뛴다
    assert {first.request_id, second.request_id} == {"r-1", "r-2"}


def test_멈춘_처리_중_작업은_다시_잡는다(conn):
    submit(conn, body())
    claim(conn)
    assert claim(conn) is None
    conn.execute("UPDATE minutes_jobs SET started_at = now() - interval '1 hour'")
    again = claim(conn, stuck_after_s=600)
    assert again.request_id == "r-1" and again.attempts == 2


def test_끝내면_요청_본문을_지운다(conn):
    submit(conn, body())
    job = claim(conn)
    finish(conn, job.id, "done", "FAILED", "CHECK_FAILED")
    status, result, code, _, payload, _, _ = row(conn)
    assert (status, result, code, payload) == ("done", "FAILED", "CHECK_FAILED", {})


def test_나중에_다시는_대기로_되돌리고_그때까지_꺼내지_않는다(conn):
    submit(conn, body())
    job = claim(conn)
    retry_later(conn, job.id, 60, "OperationalError")
    assert row(conn)[0] == "queued" and row(conn)[6] == "OperationalError"
    assert claim(conn) is None
    conn.execute("UPDATE minutes_jobs SET run_after = now() - interval '1 second'")
    assert claim(conn).attempts == 2


def test_정리는_보관_기간이_지난_끝난_줄만_지운다(conn):
    for rid in ("old-done", "old-queued", "new-done"):
        submit(conn, body(rid))
    for rid in ("old-done", "new-done"):
        job = conn.execute("SELECT id FROM minutes_jobs WHERE request_id = %s", (rid,)).fetchone()[0]
        finish(conn, job, "done", "SUCCEEDED")
    conn.execute("UPDATE minutes_jobs SET updated_at = now() - interval '8 days' WHERE request_id LIKE 'old-%%'")
    assert cleanup_finished(conn, keep_days=jobs.KEEP_DAYS) == 1
    left = {r[0] for r in conn.execute("SELECT request_id FROM minutes_jobs")}
    assert left == {"old-queued", "new-done"}
