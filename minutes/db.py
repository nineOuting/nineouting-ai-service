"""회의록 초안 DB 접속. 주소는 환경변수로만 받는다 (MINUTES_DATABASE_URL). 주소에 비밀번호가 들어 있으므로 출력하지 않는다.

RAG 와 같은 PostgreSQL 을 가리켜도 되고 다른 DB 여도 된다. 표는 minutes_ 로 시작해 RAG 표와 겹치지 않는다.
"""
import os

DATABASE_ENV = "MINUTES_DATABASE_URL"
TEST_DATABASE_ENVS = ("MINUTES_TEST_DATABASE_URL", "RAG_TEST_DATABASE_URL")   # 시험은 RAG 시험 DB 를 같이 써도 된다


def timeout_options(statement_timeout_ms=None, idle_tx_timeout_ms=None):
    """연결별 시간 제한 (libpq options 문자열). 멈춘 트랜잭션이 잠금을 오래 잡지 않게 하려는 것."""
    options = []
    if statement_timeout_ms:
        options.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    if idle_tx_timeout_ms:
        options.append(f"-c idle_in_transaction_session_timeout={int(idle_tx_timeout_ms)}")
    return " ".join(options)


def database_url_for_tests(env=None):
    env = os.environ if env is None else env
    return next((env[k] for k in TEST_DATABASE_ENVS if env.get(k)), None)


def connect(url=None, env=DATABASE_ENV, statement_timeout_ms=None, idle_tx_timeout_ms=None, connect_timeout_s=None):
    """autocommit 연결을 돌려준다. 트랜잭션은 conn.transaction() 으로 연다."""
    import psycopg

    url = url or os.environ.get(env)
    if not url:
        raise RuntimeError(f"환경변수 {env} 가 없음")
    options = timeout_options(statement_timeout_ms, idle_tx_timeout_ms)
    kwargs = {"options": options} if options else {}
    if connect_timeout_s:
        kwargs["connect_timeout"] = int(connect_timeout_s)
    return psycopg.connect(url, autocommit=True, **kwargs)
