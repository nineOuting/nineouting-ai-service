"""회의록 초안 작업자: 대기열에서 꺼내 → 초안 만들기(make_draft) → 결과 콜백 → 끝내기. 명세 2.3·3.3

  - make_draft 는 AI 실패를 FAILED 결과로 돌려준다. 예외가 아니므로 그대로 콜백한다
  - 예상하지 못한 예외(코드 오류·DB 끊김 등)는 일시 실패로 보고 30초·2분 뒤 다시 한다.
    3번째도 실패하면 FAILED / INTERNAL_ERROR 를 콜백한다
  - 콜백을 끝내 못 보내면 작업을 failed 로 끝낸다. 같은 requestId 로 다시 오면 새로 받는다 (jobs.submit)
  - 끝나면 요청 본문(회의 내용)을 지운다. 로그에는 작업·요청 번호, 시도 횟수, 결과, 오류 코드, 걸린 시간만 남긴다

실행: 서버 안에서 스레드로 (runtime), 또는 따로 .venv/bin/python -m minutes.worker
설정: MINUTES_DATABASE_URL, MINUTES_CALLBACK_ALLOWED_HOSTS, MINUTES_LLM_TIMEOUT_S(기본 60),
      MINUTES_PROMPT_VERSION(없으면 가장 큰 번호), AI 공급자·모델·키는 RAG 답변과 같은 RAG_LLM_* 를 쓴다
"""
import logging
import os
import signal
import threading
import time

from rag.callback import CallbackSender, allowed_hosts_from_env

from .draft import INTERNAL_ERROR, MESSAGES, failed, make_draft
from .jobs import KEEP_DAYS, claim, cleanup_finished, finish, retry_later

log = logging.getLogger("minutes.worker")

MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (30, 120)       # 1번째 실패 뒤 30초, 2번째 실패 뒤 2분
POLL_S = 1.0                     # 할 일이 없을 때 다시 묻는 간격
CLEANUP_EVERY_S = 3600           # 끝난 작업 줄 정리 주기
DEFAULT_LLM_TIMEOUT_S = 60.0


class Worker:
    def __init__(self, conn, llm, callback, timeout_s=DEFAULT_LLM_TIMEOUT_S, prompt=None,
                 max_attempts=MAX_ATTEMPTS, retry_delays_s=RETRY_DELAYS_S, connect=None):
        """connect 를 주면 conn 이 없거나 끊겼을 때 run() 이 그것으로 다시 접속한다 (conn=None 으로 시작해도 됨)."""
        self.conn = conn
        self.llm = llm
        self.callback = callback
        self.timeout_s = timeout_s
        self.prompt = prompt
        self.max_attempts = max_attempts
        self.retry_delays_s = tuple(retry_delays_s)
        self._connect = connect

    def process_one(self) -> bool:
        """작업 하나를 처리했으면 True, 꺼낼 것이 없으면 False."""
        job = claim(self.conn)
        if job is None:
            return False
        started = time.monotonic()
        tag = f"job={job.id} request_id={job.request_id} minutes={job.minutes_id} attempt={job.attempts} trace={job.trace_id}"

        last_error = None
        if job.attempts > self.max_attempts:          # 멈춘 작업을 다시 잡다 보면 횟수를 넘을 수 있다
            result, last_error = failed(job.request_id, INTERNAL_ERROR, MESSAGES[INTERNAL_ERROR]), "시도 횟수 초과"
        else:
            try:
                result = make_draft(job.payload, self.llm, timeout_s=self.timeout_s, prompt=self.prompt)
            except Exception as e:                    # 일시 실패로 본다
                if job.attempts < self.max_attempts:
                    delay = self.retry_delays_s[min(job.attempts, len(self.retry_delays_s)) - 1]
                    retry_later(self.conn, job.id, delay, type(e).__name__)
                    log.warning("일시 실패, %s초 뒤 다시 %s error=%s", delay, tag, type(e).__name__)
                    return True
                result = failed(job.request_id, INTERNAL_ERROR, MESSAGES[INTERNAL_ERROR])
                last_error = type(e).__name__

        code = (result.get("error") or {}).get("code")
        sent = self.callback.send(job.callback_url, result, job.trace_id, ref=f"minutes_request={job.request_id}")
        if not sent:
            last_error = f"{last_error} / callback_failed" if last_error else "callback_failed"
        finish(self.conn, job.id, "done" if sent else "failed", result["status"], code, last_error)
        log.info("처리 끝 %s result=%s error=%s callback=%s 걸린시간=%.1fs",
                 tag, result["status"], code, "ok" if sent else "failed", time.monotonic() - started)
        return True

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


def from_env(env=None, llm=None, prompt=None):
    """환경변수로 작업자를 만든다. llm 을 주지 않으면 RAG_LLM_* 설정으로 만든다 (설정이 틀리면 LLMConfigError).
    DB 에는 여기서 접속하지 않는다. run() 이 처음 돌 때 접속하고, 끊기면 다시 접속한다 (DB 가 늦게 떠도 서버는 뜬다)."""
    from functools import partial

    from .db import connect
    from .placement import load_prompt

    env = os.environ if env is None else env
    connect_db = partial(connect, url=env.get("MINUTES_DATABASE_URL"),
                         statement_timeout_ms=env.get("MINUTES_DB_STATEMENT_TIMEOUT_MS", 30000),
                         idle_tx_timeout_ms=env.get("MINUTES_DB_IDLE_TX_TIMEOUT_MS", 60000),
                         connect_timeout_s=env.get("MINUTES_DB_CONNECT_TIMEOUT_S", 10))
    if llm is None:
        from rag.llm import make_llm
        llm, _ = make_llm(env)                        # 시간 제한은 MINUTES_LLM_TIMEOUT_S 를 따로 쓴다
    allowed = allowed_hosts_from_env(env, key="MINUTES_CALLBACK_ALLOWED_HOSTS")
    if allowed is None:
        log.warning("MINUTES_CALLBACK_ALLOWED_HOSTS 가 비어 있어 모든 콜백 주소를 허용함 (개발용)")
    prompt = prompt or load_prompt(env.get("MINUTES_PROMPT_VERSION") or None)
    timeout_s = float(env.get("MINUTES_LLM_TIMEOUT_S", DEFAULT_LLM_TIMEOUT_S))
    return Worker(None, llm, CallbackSender(allowed=allowed), timeout_s=timeout_s, prompt=prompt, connect=connect_db)


def start_in_thread(worker, stop=None):
    """API 프로세스 안에서 작업자를 스레드로 띄운다. (thread, stop) 을 돌려준다."""
    stop = stop or threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), name="minutes-worker", daemon=True)
    thread.start()
    return thread, stop


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = from_env()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("작업자 시작 (지시문 %s, 시간 제한 %.0f초)", worker.prompt.version, worker.timeout_s)
    worker.run(stop)
    log.info("작업자 멈춤")


if __name__ == "__main__":
    main()
