"""파이프라인 단계 사이에 넘기는 데이터."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Piece:
    """로더 출력. 형식(회의록·PDF·워드·엑셀)과 상관없이 같은 모양이다."""
    content: str                 # 원문 그대로. 답변 근거(citations[].text)로 나간다
    location: dict               # API 의 location 객체 (type, label, ...)
    context: str | None = None   # 임베딩 글 앞에 붙일 맥락 (회의록 주제, 문서 제목 경로 등)
    kind: str | None = None      # 출처 칸 (임시 형식 회의록: summary / decisions / action_items, 확정 회의록: section)


@dataclass(frozen=True)
class Chunk:
    """나누기 출력. 임베딩 입력 상한 안에 들어간다."""
    chunk_index: int             # 문서 안에서 0부터
    content: str
    embed_text: str              # 임베딩에 넣을 글 (모델 접두어는 임베더가 붙인다)
    location: dict
    token_count: int             # 모델 접두어·특수 토큰을 포함한 수
    kind: str | None = None
