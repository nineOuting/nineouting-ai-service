"""DB 접속. 주소는 환경변수로만 받는다 (RAG_DATABASE_URL). 주소에 비밀번호가 들어 있으므로 출력하지 않는다."""
import os


def timeout_options(statement_timeout_ms=None, idle_tx_timeout_ms=None):
    """연결별 시간 제한 (libpq options 문자열). 멈춘 트랜잭션이 잠금을 오래 잡지 않게 하려는 것."""
    options = []
    if statement_timeout_ms:
        options.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    if idle_tx_timeout_ms:
        options.append(f"-c idle_in_transaction_session_timeout={int(idle_tx_timeout_ms)}")
    return " ".join(options)


def connect(url=None, env="RAG_DATABASE_URL", register=True, statement_timeout_ms=None, idle_tx_timeout_ms=None):
    """autocommit 연결을 돌려준다. 트랜잭션은 conn.transaction() 으로 연다.
    register=True 면 vector 형을 등록한다 (vector 확장이 있어야 함. 마이그레이션 실행기는 False).
    시간 제한을 주면 그 연결에만 적용된다."""
    import psycopg

    url = url or os.environ.get(env)
    if not url:
        raise RuntimeError(f"환경변수 {env} 가 없음")
    options = timeout_options(statement_timeout_ms, idle_tx_timeout_ms)
    kwargs = {"options": options} if options else {}
    conn = psycopg.connect(url, autocommit=True, **kwargs)
    if register:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    return conn
