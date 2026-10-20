"""문서 접수·저장·삭제.

나중에 API 가 register() 를, 작업자가 store() / mark_failed() 를 부른다.

버전 규칙
  - 마지막으로 접수한 버전이 현재 버전이다 (docVersion 은 해시라 순서를 비교할 수 없다)
  - 같은 docId+docVersion 이 다시 오면 결과는 하나다. 단 failed 였으면 다시 처리한다
  - store() 는 문서 행을 잠근 뒤, 그 사이 문서가 지워졌거나 더 새 버전이 접수됐으면 StaleJob 으로 중단한다
  - 교체는 한 트랜잭션: 이 문서의 기존 조각을 지우고 새 조각을 넣는다. 끝나기 전까지는 옛 버전이 검색된다
  - 새 버전이 실패하면 옛 버전 조각은 남는다 (마지막으로 성공한 버전이 계속 검색된다)
"""
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .models import Chunk


class StaleJob(Exception):
    """처리하던 문서가 지워졌거나, 더 새 버전이 접수돼 결과를 버린다."""


class WorkspaceMismatch(ValueError):
    """같은 docId 가 다른 workspaceId 로 들어왔다."""


@dataclass(frozen=True)
class DocumentMeta:
    doc_id: int
    doc_version: str
    workspace_id: int
    source_type: str             # upload / minutes
    title: str


@dataclass(frozen=True)
class RegisterResult:
    status: str                  # 접수 후 문서 상태
    enqueue: bool                # True 면 호출한 쪽이 처리 작업을 넣는다. 같은 버전이 이미 있으면 False


def register(conn, doc: DocumentMeta) -> RegisterResult:
    """접수. 새 문서·새 버전·실패한 버전의 재시도면 queued 로 바꾸고 enqueue=True.

    동시 접수: 행이 없을 때는 SELECT ... FOR UPDATE 로 잠글 것이 없다. 그래서 먼저
    INSERT ... ON CONFLICT DO NOTHING 으로 행을 확보한다. 같은 docId 를 동시에 넣는 쪽은
    먼저 넣은 트랜잭션이 끝날 때까지 기다린 뒤 아무것도 넣지 않고, 이어서 그 행을 잠가 본다.
    """
    with conn.transaction():
        # INSERT 와 SELECT 사이에 다른 쪽이 그 문서를 지우면 행이 없을 수 있다. 그때는 다시 넣어 본다
        for _ in range(3):
            inserted = conn.execute(
                "INSERT INTO rag_documents (doc_id, doc_version, workspace_id, source_type, title, status) "
                "VALUES (%s, %s, %s, %s, %s, 'queued') ON CONFLICT (doc_id) DO NOTHING",
                (doc.doc_id, doc.doc_version, doc.workspace_id, doc.source_type, doc.title)).rowcount
            if inserted:
                return RegisterResult("queued", True)
            row = conn.execute("SELECT doc_version, status, workspace_id FROM rag_documents "
                               "WHERE doc_id = %s FOR UPDATE", (doc.doc_id,)).fetchone()
            if row is not None:
                break
        else:
            raise RuntimeError(f"docId {doc.doc_id}: 접수 중 같은 문서의 추가·삭제가 반복됨")

        version, status, workspace_id = row
        if workspace_id != doc.workspace_id:
            raise WorkspaceMismatch(f"docId {doc.doc_id} 의 workspaceId 가 기존과 다름")
        if version == doc.doc_version and status != "failed":
            return RegisterResult(status, False)
        conn.execute("UPDATE rag_documents SET doc_version = %s, source_type = %s, title = %s, "
                     "status = 'queued', error_code = NULL, updated_at = now() WHERE doc_id = %s",
                     (doc.doc_version, doc.source_type, doc.title, doc.doc_id))
        return RegisterResult("queued", True)


def store(conn, doc_id: int, doc_version: str, chunks: Sequence[Chunk], vectors, embed_model: str) -> int:
    """조각을 저장하고 문서를 ready 로 바꾼다. 저장한 조각 수를 돌려준다."""
    if len(chunks) != len(vectors):
        raise ValueError(f"조각 {len(chunks)}개와 벡터 {len(vectors)}개의 수가 다름")
    from psycopg.types.json import Jsonb

    with conn.transaction():
        row = conn.execute("SELECT doc_version, workspace_id FROM rag_documents "
                           "WHERE doc_id = %s FOR UPDATE", (doc_id,)).fetchone()
        if row is None or row[0] != doc_version:
            raise StaleJob(f"docId {doc_id}: 문서가 지워졌거나 더 새 버전이 접수됨")
        workspace_id = row[1]
        conn.execute("DELETE FROM rag_chunks WHERE doc_id = %s", (doc_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO rag_chunks (doc_id, doc_version, workspace_id, chunk_index, content, "
                "location, token_count, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(doc_id, doc_version, workspace_id, c.chunk_index, c.content, Jsonb(c.location),
                  c.token_count, np.asarray(v, dtype=np.float32)) for c, v in zip(chunks, vectors)])
        conn.execute("UPDATE rag_documents SET status = 'ready', error_code = NULL, chunk_count = %s, "
                     "embed_model = %s, updated_at = now() WHERE doc_id = %s",
                     (len(chunks), embed_model, doc_id))
    return len(chunks)


def mark_failed(conn, doc_id: int, doc_version: str, error_code: str) -> bool:
    """그 버전이 아직 현재 버전일 때만 failed 로 바꾼다. 바꿨으면 True."""
    cur = conn.execute("UPDATE rag_documents SET status = 'failed', error_code = %s, updated_at = now() "
                       "WHERE doc_id = %s AND doc_version = %s", (error_code, doc_id, doc_version))
    return cur.rowcount == 1


def get_status(conn, doc_id: int) -> dict | None:
    """상태 조회 API 의 응답 모양. 문서가 없으면 None."""
    row = conn.execute("SELECT doc_version, status, error_code, chunk_count FROM rag_documents "
                       "WHERE doc_id = %s", (doc_id,)).fetchone()
    if row is None:
        return None
    return {"docId": doc_id, "docVersion": row[0], "status": row[1], "errorCode": row[2], "chunkCount": row[3]}


def mark_processing(conn, doc_id: int, doc_version: str) -> bool:
    """작업자가 처리를 시작할 때. 그 버전이 아직 현재 버전이면 processing 으로 바꾸고 True."""
    cur = conn.execute("UPDATE rag_documents SET status = 'processing', updated_at = now() "
                       "WHERE doc_id = %s AND doc_version = %s AND status IN ('queued', 'processing')",
                       (doc_id, doc_version))
    return cur.rowcount == 1


def is_current(conn, doc_id: int, doc_version: str) -> bool:
    """문서가 남아 있고 그 버전이 현재 버전인가."""
    return conn.execute("SELECT 1 FROM rag_documents WHERE doc_id = %s AND doc_version = %s",
                        (doc_id, doc_version)).fetchone() is not None


def delete_document(conn, doc_id: int) -> None:
    """문서와 조각, 그 문서의 작업을 지운다. 없어도 오류가 아니다.
    처리 중인 작업은 저장할 때 StaleJob 으로 버려진다."""
    with conn.transaction():
        conn.execute("DELETE FROM rag_jobs WHERE doc_id = %s", (doc_id,))
        conn.execute("DELETE FROM rag_documents WHERE doc_id = %s", (doc_id,))


def delete_workspace(conn, workspace_id: int) -> int:
    """워크스페이스의 문서와 조각, 작업을 지운다. 지운 문서 수를 돌려준다."""
    with conn.transaction():
        conn.execute("DELETE FROM rag_jobs WHERE workspace_id = %s", (workspace_id,))
        return conn.execute("DELETE FROM rag_documents WHERE workspace_id = %s", (workspace_id,)).rowcount
