"""작업자 시험 (시험 DB 필요). 꺼내기 → 초안 만들기 → 결과 콜백 → 끝내기. AI 와 콜백은 가짜를 쓴다.
rag/tests/test_worker.py 와 이름이 겹치지 않게 따로 이름을 붙였다."""
import logging
import threading

from minutes.draft import INTERNAL_ERROR
from minutes.jobs import ACCEPTED, submit
from minutes.placement import load_prompt
from minutes.worker import Worker, from_env, start_in_thread
from minutes_fakes import GOOD, ScriptLLM, load_request


class FakeCallback:
    """받은 콜백을 기록한다. ok 로 성공·실패를 정한다."""

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, url, body, request_id=None, ref=None):
        self.sent.append({"url": url, "body": body, "request_id": request_id, "ref": ref})
        return self.ok


class Boom:
    """예상하지 못한 예외를 내는 AI (LLMError 가 아니라 다시 시도 대상)."""
    name = model = "boom"

    def generate(self, system, user, timeout_s):
        raise RuntimeError("bug")


def job_row(conn, request_id):
    return conn.execute("SELECT status, result_status, error_code, attempts, payload, last_error "
                        "FROM minutes_jobs WHERE request_id = %s", (request_id,)).fetchone()


def worker(conn, llm, callback=None, **kw):
    return Worker(conn, llm, callback or FakeCallback(), timeout_s=12, retry_delays_s=(0, 0), **kw)


def put(conn, trace="t-1"):
    body = load_request()
    submit(conn, body, trace_id=trace)
    return body


def test_꺼낼_것이_없으면_False(conn):
    assert worker(conn, ScriptLLM()).process_one() is False


def test_초안을_만들어_콜백하고_요청_본문을_지운다(conn):
    body, cb = put(conn), FakeCallback()
    assert worker(conn, ScriptLLM(GOOD), cb).process_one() is True
    sent = cb.sent[0]
    assert sent["url"] == body["callbackUrl"] and sent["request_id"] == "t-1"
    assert sent["ref"] == f"minutes_request={body['requestId']}"
    assert sent["body"]["status"] == "SUCCEEDED" and sent["body"]["requestId"] == body["requestId"]
    assert [s["title"] for s in sent["body"]["sections"]] == body["template"]["items"]
    assert job_row(conn, body["requestId"])[:4] == ("done", "SUCCEEDED", None, 1)
    assert job_row(conn, body["requestId"])[4] == {}


def test_AI가_실패하면_실패_결과를_콜백하고_같은_번호로_다시_받는다(conn):
    body, cb = put(conn), FakeCallback()
    worker(conn, ScriptLLM("x", "x", "x"), cb).process_one()
    assert cb.sent[0]["body"]["error"]["code"] == "CHECK_FAILED"
    assert job_row(conn, body["requestId"])[:3] == ("done", "FAILED", "CHECK_FAILED")
    assert submit(conn, body) == ACCEPTED


def test_예상하지_못한_오류는_다시_시도하고_끝내_안되면_INTERNAL_ERROR(conn):
    body, cb = put(conn), FakeCallback()
    w = worker(conn, Boom(), cb)
    for attempt in (1, 2):
        w.process_one()
        status, _, _, attempts, _, last_error = job_row(conn, body["requestId"])
        assert (status, attempts, last_error) == ("queued", attempt, "RuntimeError")
        assert cb.sent == []
    w.process_one()
    assert cb.sent[0]["body"]["error"]["code"] == INTERNAL_ERROR
    assert job_row(conn, body["requestId"])[:3] == ("done", "FAILED", INTERNAL_ERROR)


def test_시도_횟수를_넘긴_작업은_INTERNAL_ERROR로_끝낸다(conn):
    body, cb = put(conn), FakeCallback()
    conn.execute("UPDATE minutes_jobs SET attempts = 3")
    llm = ScriptLLM()
    worker(conn, llm, cb).process_one()
    assert llm.calls == [] and cb.sent[0]["body"]["error"]["code"] == INTERNAL_ERROR
    assert job_row(conn, body["requestId"])[5] == "시도 횟수 초과"


def test_콜백을_못_보내면_failed로_끝내고_같은_번호로_다시_받는다(conn):
    body = put(conn)
    worker(conn, ScriptLLM(GOOD), FakeCallback(ok=False)).process_one()
    status, result, _, _, payload, last_error = job_row(conn, body["requestId"])
    assert (status, result, payload, last_error) == ("failed", "SUCCEEDED", {}, "callback_failed")
    assert submit(conn, body) == ACCEPTED


def test_로그에_회의_내용이_남지_않는다(conn, caplog):
    caplog.set_level(logging.INFO, logger="minutes")
    put(conn)
    worker(conn, ScriptLLM(GOOD)).process_one()
    assert "처리 끝" in caplog.text and "result=SUCCEEDED" in caplog.text
    for word in ["배포", "예산", "회식", "김철수", "안건별 논의"]:
        assert word not in caplog.text


def test_스레드로_돌다가_멈추라면_멈춘다(conn):
    body, cb = put(conn), FakeCallback()
    done = threading.Event()

    class Notify(FakeCallback):
        def send(self, *a, **kw):
            done.set()
            return cb.send(*a, **kw)

    thread, stop = start_in_thread(worker(conn, ScriptLLM(GOOD), Notify()))
    assert done.wait(5)
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and cb.sent[0]["body"]["requestId"] == body["requestId"]


def test_환경변수로_작업자를_만든다():
    env = {"MINUTES_DATABASE_URL": "postgresql://unused", "MINUTES_CALLBACK_ALLOWED_HOSTS": "minutes-service:8084",
           "MINUTES_LLM_TIMEOUT_S": "45", "MINUTES_PROMPT_VERSION": "v3"}
    w = from_env(env, llm=ScriptLLM())
    assert w.conn is None and w.timeout_s == 45
    assert w.prompt.version == "placement-v3"
    assert w.callback.allowed == frozenset({"minutes-service:8084"})
    assert from_env({}, llm=ScriptLLM()).prompt == load_prompt()          # 버전을 안 주면 가장 큰 번호


class StopAfterFirstWait(threading.Event):
    """반복을 한 바퀴만 돌게 한다 (오류 뒤 기다리는 순간 멈춤 표시)."""

    def wait(self, timeout=None):
        self.set()
        return True


def test_대기열_표가_없으면_마이그레이션_안내를_로그에_남기고_멈추지_않는다(other_conn, caplog):
    other_conn.execute("SET search_path TO pg_catalog")       # 표를 지우지 않고 안 보이게 한다
    with caplog.at_level("ERROR", logger="minutes.worker"):
        worker(other_conn, ScriptLLM()).run(StopAfterFirstWait())
    assert "error=UndefinedTable (대기열 표 없음, python -m minutes.migrate 필요)" in caplog.text
