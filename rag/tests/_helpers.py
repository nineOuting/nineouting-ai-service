"""저장·검색 시험의 공용 도우미."""
from rag.models import Chunk
from rag.store import DocumentMeta, register, store

WS, OTHER_WS = 10, 20


def doc(doc_id=1, version="v1", ws=WS):
    return DocumentMeta(doc_id=doc_id, doc_version=version, workspace_id=ws, source_type="minutes", title="주간 회의")


def chunks_of(*texts):
    loc = {"type": "time", "startMs": 0, "endMs": 1000, "label": "00:00~00:01"}
    return [Chunk(i, t, t, loc, len(t)) for i, t in enumerate(texts)]


def put(conn, emb, d, *texts):
    """접수 후 바로 저장 (작업자가 처리한 것과 같음)."""
    register(conn, d)
    cs = chunks_of(*texts)
    return store(conn, d.doc_id, d.doc_version, cs, emb.embed_passages([c.embed_text for c in cs]), emb.model_id)


def chunk_rows(conn, doc_id):
    return conn.execute("SELECT doc_version, chunk_index, content FROM rag_chunks WHERE doc_id = %s "
                        "ORDER BY chunk_index", (doc_id,)).fetchall()
