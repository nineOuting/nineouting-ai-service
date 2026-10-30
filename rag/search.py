"""조각 검색. 항상 workspaceId 와 docIds 로 거른다.

  - docIds 가 비면 DB 에 묻지 않고 빈 목록 (필터를 건너뛰는 길이 없다)
  - workspace_id 는 조각과 문서 양쪽에서 거른다
  - 질문과 같은 모델로 만든 문서만 본다 (rag_documents.embed_model)
  - 유사도 하한은 아직 없다 (측정 전). 필터 후 조각이 없을 때만 빈 목록이다
"""
from dataclasses import dataclass
from typing import Iterable

import numpy as np

MAX_TOP_K = 20

_SQL = """
SELECT c.doc_id, d.source_type, d.title, c.content, c.location,
       1 - (c.embedding <=> %(q)s) AS score
FROM rag_chunks c
JOIN rag_documents d ON d.doc_id = c.doc_id
WHERE c.workspace_id = %(ws)s AND d.workspace_id = %(ws)s
  AND c.doc_id = ANY(%(ids)s)
  AND d.embed_model = %(model)s
ORDER BY c.embedding <=> %(q)s
LIMIT %(k)s
"""


@dataclass(frozen=True)
class Hit:
    doc_id: int
    source_type: str
    title: str
    content: str
    location: dict
    score: float                 # 코사인 유사도 (1 에 가까울수록 가까움)


def search(conn, embedder, workspace_id: int, doc_ids: Iterable[int], query: str, top_k: int = 5) -> list[Hit]:
    ids = sorted(set(doc_ids))
    if not ids:
        return []
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"topK 는 1~{MAX_TOP_K}")
    q = np.asarray(embedder.embed_query(query), dtype=np.float32)
    rows = conn.execute(_SQL, {"q": q, "ws": workspace_id, "ids": ids,
                               "model": embedder.model_id, "k": top_k}).fetchall()
    return [Hit(*r[:5], score=float(r[5])) for r in rows]
