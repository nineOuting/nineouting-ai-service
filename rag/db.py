"""DB 접속. 주소는 환경변수로만 받는다 (RAG_DATABASE_URL). 주소에 비밀번호가 들어 있으므로 출력하지 않는다."""
import os


def connect(url=None, env="RAG_DATABASE_URL", register=True):
    """autocommit 연결을 돌려준다. 트랜잭션은 conn.transaction() 으로 연다.
    register=True 면 vector 형을 등록한다 (vector 확장이 있어야 함. 마이그레이션 실행기는 False)."""
    import psycopg

    url = url or os.environ.get(env)
    if not url:
        raise RuntimeError(f"환경변수 {env} 가 없음")
    conn = psycopg.connect(url, autocommit=True)
    if register:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    return conn
