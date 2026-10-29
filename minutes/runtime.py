"""server/main.py 에 회의록 초안을 붙이는 곳.

server/main.py 는 MINUTES_DATABASE_URL 이 있을 때만 이 모듈을 불러온다. 없으면 회의록 초안 없이 지금처럼 뜬다.
RAG 와 따로 켜고 끈다. 같은 PostgreSQL 을 써도 되지만 연결 풀·마이그레이션·설정은 따로다.

  setup(app, env)  라우터를 앱에 붙이고 MinutesRuntime 을 돌려준다 (연결 풀은 아직 열지 않음)
  runtime.start()  서버 시작 때: 연결 풀을 열고, MINUTES_WORKER_IN_PROCESS 가 0 이 아니면 작업자 스레드를 띄운다
  runtime.stop()   서버 종료 때: 작업자는 처리 중인 작업을 마치고 멈추고, 연결 풀을 닫는다

AI 설정(MINUTES_LLM_*, 없으면 RAG_LLM_*. llm_config.py)이 틀리면 서버는 그대로 뜨고 초안 요청만 503 으로 답한다 (요약·RAG 까지 멈추지 않게).
"""
import logging

from .api import PREFIX, REQUEST_ID_HEADER, make_router
from .db import timeout_options

log = logging.getLogger("minutes.runtime")


class MinutesRuntime:
    def __init__(self, pool, env, llm=None, prompt=None, relayout_prompt=None, table_prompt=None):
        self.pool = pool
        self.env = env
        self.llm = llm                          # None 이면 AI 설정 오류. 작업자를 띄우지 않는다
        self.prompt = prompt
        self.relayout_prompt = relayout_prompt
        self.table_prompt = table_prompt
        self.worker = None
        self.thread = None
        self.stop_event = None

    def start(self):
        self.pool.open(wait=False)              # DB 가 아직 안 떠 있어도 서버는 뜬다 (그동안 초안 주소는 503)
        if self.llm is None:
            log.error("AI 설정 오류로 회의록 초안 작업자를 띄우지 않음")
            return
        if self.env.get("MINUTES_WORKER_IN_PROCESS", "1") == "0":
            log.info("회의록 초안 작업자는 이 프로세스에서 띄우지 않음 (python -m minutes.worker 로 따로)")
            return
        from .worker import from_env, start_in_thread
        self.worker = from_env(self.env, llm=self.llm, prompt=self.prompt, relayout_prompt=self.relayout_prompt,
                               table_prompt=self.table_prompt)
        self.thread, self.stop_event = start_in_thread(self.worker)
        log.info("회의록 초안 작업자 시작 (지시문 %s, %s, %s, 시간 제한 %.0f초)", self.prompt.version,
                 self.relayout_prompt.version, self.table_prompt.version, self.worker.timeout_s)

    def stop(self, timeout_s=90):
        """처리 중인 작업을 마칠 때까지 기다린다. AI 응답 시간 제한(기본 60초)보다 길게 둔다."""
        if self.stop_event is not None:
            self.stop_event.set()
            self.thread.join(timeout=timeout_s)
            self.worker.close()
            log.info("회의록 초안 작업자 멈춤")
        self.pool.close()


def setup(app, env) -> MinutesRuntime:
    from psycopg_pool import ConnectionPool

    from rag.callback import allowed_hosts_from_env
    from rag.llm import LLMConfigError

    from .llm_config import make_minutes_llm
    from .placement import RELAYOUT_KIND, load_prompt
    from .table import TABLE_KIND

    options = timeout_options(env.get("MINUTES_DB_STATEMENT_TIMEOUT_MS", 30000),
                              env.get("MINUTES_DB_IDLE_TX_TIMEOUT_MS", 60000))
    pool = ConnectionPool(env["MINUTES_DATABASE_URL"], min_size=1, max_size=int(env.get("MINUTES_DB_POOL_MAX", "3")),
                          timeout=float(env.get("MINUTES_DB_POOL_TIMEOUT_S", "5")), open=False,
                          kwargs={"autocommit": True, "options": options}, name="minutes")
    allowed = allowed_hosts_from_env(env, key="MINUTES_CALLBACK_ALLOWED_HOSTS")
    if allowed is None:
        log.warning("MINUTES_CALLBACK_ALLOWED_HOSTS 가 비어 있어 모든 콜백 주소를 허용함 (개발용)")

    llm, config_error, prompt, relayout_prompt, table_prompt = None, None, None, None, None
    try:
        llm, _, source = make_minutes_llm(env)
        prompt = load_prompt(env.get("MINUTES_PROMPT_VERSION") or None)
        relayout_prompt = load_prompt(env.get("MINUTES_RELAYOUT_PROMPT_VERSION") or None, RELAYOUT_KIND)
        table_prompt = load_prompt(env.get("MINUTES_TABLE_PROMPT_VERSION") or None, TABLE_KIND)
    except (LLMConfigError, ValueError) as e:                 # 지시문 오류(PromptError)도 ValueError
        llm, config_error = None, str(e)
        log.error("회의록 초안 설정 오류: %s", e)
    else:
        log.info("회의록 초안 AI %s/%s (%s_* 설정), 지시문 %s, %s, %s", llm.name, llm.model, source, prompt.version,
                 relayout_prompt.version, table_prompt.version)

    app.include_router(make_router(pool, allowed, env.get("MINUTES_REQUEST_ID_HEADER", REQUEST_ID_HEADER),
                                   env.get("MINUTES_API_PREFIX", PREFIX), config_error=config_error))
    return MinutesRuntime(pool, env, llm, prompt, relayout_prompt, table_prompt)
