"""조각 검색. 항상 workspaceId 와 문서 목록으로 거른다.

  - 문서는 (sourceType, docId) 짝으로 구분한다 (챗봇 명세 v0.2 9장). 회의록 200번과 첨부 자료 200번은 다른 문서다
  - 문서 목록이 비면 DB 에 묻지 않고 빈 목록 (필터를 건너뛰는 길이 없다)
  - workspace_id 는 조각과 문서 양쪽에서 거른다
  - 질문과 같은 모델로 만든 문서만 본다 (rag_documents.embed_model)
  - 유사도 하한은 아직 없다 (측정 전). 필터 후 조각이 없을 때만 빈 목록이다
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import numpy as np

MAX_TOP_K = 20

_SQL = """
SELECT c.doc_id, d.source_type, d.title, c.content, c.location,
       1 - (c.embedding <=> %(q)s) AS score, d.meeting_started_at, d.received_at
FROM rag_chunks c
JOIN rag_documents d ON d.source_type = c.source_type AND d.doc_id = c.doc_id
WHERE c.workspace_id = %(ws)s AND d.workspace_id = %(ws)s
  AND (c.source_type, c.doc_id) IN (SELECT * FROM unnest(%(types)s::text[], %(ids)s::bigint[]))
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
    meeting_started_at: datetime | None = None   # 회의록의 회의 시작 시각 (SCRUM-234)
    received_at: datetime | None = None          # 그 버전을 받은 시각. 첨부 자료는 "올린 날"


def search(conn, embedder, workspace_id: int, docs: Iterable[tuple[str, int]], query: str,
           top_k: int = 5) -> list[Hit]:
    """docs: (sourceType, docId) 짝 목록. 같은 짝은 하나로 본다."""
    pairs = sorted(set(docs))
    if not pairs:
        return []
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"topK 는 1~{MAX_TOP_K}")
    q = np.asarray(embedder.embed_query(query), dtype=np.float32)
    rows = conn.execute(_SQL, {"q": q, "ws": workspace_id, "types": [s for s, _ in pairs],
                               "ids": [d for _, d in pairs], "model": embedder.model_id, "k": top_k}).fetchall()
    return [Hit(*r[:5], score=float(r[5]), meeting_started_at=r[6], received_at=r[7]) for r in rows]
