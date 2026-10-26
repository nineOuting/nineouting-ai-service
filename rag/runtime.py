"""server/main.py 에 RAG 를 붙이는 곳.

server/main.py 는 RAG_DATABASE_URL 이 있을 때만 이 모듈을 불러온다. 요약만 쓰는 환경은 RAG 의존성 없이 그대로 뜬다.

  setup(app, env)  라우터를 앱에 붙이고 RagRuntime 을 돌려준다 (연결 풀은 아직 열지 않음)
  runtime.start()  서버 시작 때: 연결 풀을 열고, 임베딩 모델을 한 번 불러오고, RAG_WORKER_IN_PROCESS 가 0 이 아니면
                   작업자 스레드를 띄운다. 모델은 작업자와 답변 API 가 같이 쓴다 (메모리를 두 번 쓰지 않게)
  runtime.stop()   서버 종료 때: 작업자는 처리 중인 작업을 마치고 멈추고, 연결 풀을 닫는다
"""
import logging

from .answer import Answerer
from .api import PREFIX, REQUEST_ID_HEADER, make_router
from .callback import allowed_hosts_from_env
from .db import timeout_options
from .download import settings_from_env as upload_settings_from_env
from .llm import LLMConfigError, make_llm

log = logging.getLogger("rag.runtime")


class RagRuntime:
    def __init__(self, pool, env):
        self.pool = pool
        self.env = env
        self.embedder = None                    # start() 에서 불러온다. 그 전에는 답변 API 가 503
        self.worker = None
        self.thread = None
        self.stop_event = None

    def start(self):
        self.pool.open(wait=False)              # DB 가 아직 안 떠 있어도 서버는 뜬다 (그동안 RAG 주소는 503)
        from .worker import embedder_from_env, from_env, start_in_thread
        self.embedder = embedder_from_env(self.env)
        if self.env.get("RAG_WORKER_IN_PROCESS", "1") == "0":
            log.info("RAG 작업자는 이 프로세스에서 띄우지 않음 (python -m rag.worker 로 따로)")
            return
        self.worker = from_env(self.env, embedder=self.embedder)
        self.thread, self.stop_event = start_in_thread(self.worker)
        log.info("RAG 작업자 시작")

    def stop(self, timeout_s=30):
        if self.stop_event is not None:
            self.stop_event.set()
            self.thread.join(timeout=timeout_s)
            self.worker.close()
            log.info("RAG 작업자 멈춤")
        self.pool.close()


def make_answerer(runtime, env) -> Answerer:
    """LLM 설정이 잘못돼도 서버는 뜨게 한다 (요약까지 멈추지 않게). 그때 답변 API 만 503 이다."""
    try:
        llm, timeout_s = make_llm(env)
    except LLMConfigError as e:
        log.error("답변 LLM 설정 오류: %s", e)
        return Answerer(lambda: runtime.embedder, config_error=str(e))
    # 지시문도 여기서 읽는다. RAG_ANSWER_PROMPT_VERSION(예: v1)으로 고르고, 없으면 가장 큰 번호
    answerer = Answerer(lambda: runtime.embedder, llm, timeout_s, prompt_version=env.get("RAG_ANSWER_PROMPT_VERSION"))
    if answerer.config_error:
        log.error("답변 설정 오류: %s", answerer.config_error)
    else:
        log.info("답변 LLM %s/%s, 지시문 %s, 시간 제한 %.0f초", llm.name, llm.model, answerer.prompt.version, timeout_s)
    return answerer


def setup(app, env) -> RagRuntime:
    from pgvector.psycopg import register_vector
    from psycopg_pool import ConnectionPool

    options = timeout_options(env.get("RAG_DB_STATEMENT_TIMEOUT_MS", 30000), env.get("RAG_DB_IDLE_TX_TIMEOUT_MS", 60000))
    pool = ConnectionPool(env["RAG_DATABASE_URL"], min_size=1, max_size=int(env.get("RAG_DB_POOL_MAX", "5")),
                          timeout=float(env.get("RAG_DB_POOL_TIMEOUT_S", "5")), open=False,
                          kwargs={"autocommit": True, "options": options}, configure=register_vector,
                          name="rag")
    allowed = allowed_hosts_from_env(env)
    if allowed is None:
        log.warning("RAG_CALLBACK_ALLOWED_HOSTS 가 비어 있어 모든 콜백 주소를 허용함 (개발용)")
    upload = upload_settings_from_env(env)
    log.info("업로드 문서 %s", "받음 (허용 저장소 %d곳)" % len(upload.allowed_hosts) if upload.enabled
             else "받지 않음 (RAG_UPLOAD_ALLOWED_HOSTS 없음)")
    runtime = RagRuntime(pool, env)
    app.include_router(make_router(pool, allowed, env.get("RAG_REQUEST_ID_HEADER", REQUEST_ID_HEADER),
                                   env.get("RAG_API_PREFIX", PREFIX), answerer=make_answerer(runtime, env),
                                   upload=upload))
    return runtime
