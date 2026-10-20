"""rag/migrations/*.sql 을 이름 순으로 한 번씩 적용한다.

실행: .venv/bin/python -m rag.migrate          (RAG_DATABASE_URL)
      .venv/bin/python -m rag.migrate --test   (RAG_TEST_DATABASE_URL)
적용 기록은 rag_schema_migrations 표에 남긴다. 파일 하나가 트랜잭션 하나다.
권한을 받지 못하면 운영에서는 SQL 파일을 그대로 실행해도 된다.
"""
import sys
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_KEY = 74_201_016          # 여러 곳에서 동시에 실행해도 한 번만 적용되게 (pg_advisory_lock)


def migrate(conn) -> list[str]:
    """적용한 파일 이름 목록을 돌려준다. conn 은 autocommit 연결."""
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rag_schema_migrations (
                          version    text        PRIMARY KEY,
                          applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {r[0] for r in conn.execute("SELECT version FROM rag_schema_migrations")}
        done = []
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.stem in applied:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO rag_schema_migrations (version) VALUES (%s)", (path.stem,))
            done.append(path.stem)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def main(argv=None):
    from .db import connect

    argv = sys.argv[1:] if argv is None else argv
    env = "RAG_TEST_DATABASE_URL" if "--test" in argv else "RAG_DATABASE_URL"
    with connect(env=env, register=False) as conn:
        done = migrate(conn)
    print(f"적용: {', '.join(done)}" if done else "적용할 마이그레이션 없음")


if __name__ == "__main__":
    main()
