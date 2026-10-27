"""회의록 초안의 자료 모양. 명세: docs/api/minutes-draft-contract.md"""
from dataclasses import dataclass

# 재료 종류. 칸 안의 줄 순서도 이 묶음 순서를 따른다 (결정·상태 → 요약 → 의견 → 할 일)
KIND_RANK = {"decision": 0, "undecided": 0, "decisionCheck": 0, "summary": 1, "opinion": 2, "todo": 3}


@dataclass(frozen=True)
class Material:
    """재료 항목 하나. 초안의 줄 하나가 된다.

    fixed_text 가 있으면 코드가 정한 글을 그대로 쓰고 AI 는 칸만 고른다.
    없으면(사람이 고치지 않은 요약 문장·의견) AI 가 칸에 맞게 다듬은 글을 쓴다.
    """
    ref: str                        # AI 입력에서 가리키는 번호 "m1", "m2" …
    agenda_id: int | None           # 작성자가 새로 쓴 줄은 None
    agenda_title: str
    kind: str                       # KIND_RANK 의 키
    item_id: str | None             # 요약 항목 id. 상태 줄(undecided, decisionCheck)은 None
    label: str                      # AI 입력에 보여 줄 종류 이름 (결정, 미결, 우려 …)
    text: str                       # AI 입력에 보여 줄 글. fixed_text 가 있으면 그것, 없으면 원래 글
    fixed_text: str | None
    evidence_seqs: tuple[int, ...]
    edited: bool
    needs_check: bool
    order: tuple[int, int, int]     # (안건 순서, 종류 순서, 같은 종류 안 순서). 칸 안 줄 순서에 쓴다
    # 다시 배치 (명세 8장, relayout.py). 처음 초안에서는 기본값 그대로다
    previous: int | None = None     # 이 재료가 된 previousLines 의 위치
    draft_edited: bool = False      # 그 줄의 draftEdited 그대로
    whole_line: bool = False        # fixed_text 가 "{안건 제목}: " 까지 붙은 줄 글 전체다 (이전 줄 글을 그대로 쓸 때)
    sourced: bool = True            # False 면 출처가 없는 줄 (작성자가 새로 쓴 줄). 결과의 sources 가 빈 목록

    @property
    def rewrite(self) -> bool:
        """AI 가 글을 다듬어야 하는 재료인가."""
        return self.fixed_text is None

    def source(self) -> dict:
        """결과의 sources·unplaced 항목."""
        return {"agendaId": self.agenda_id, "kind": self.kind, "id": self.item_id}

    def sources(self) -> list[dict]:
        return [self.source()] if self.sourced else []


TEXT, TABLE = "text", "table"


@dataclass(frozen=True)
class Field:
    """양식 필드 하나 (명세 5.8). v0.6 요청(template.items)의 이름은 글·AI 채움 필드로 읽는다."""
    title: str
    type: str = TEXT
    ai_fill: bool = True
    content: str = ""               # 사용자가 채우는 필드에 이미 써 둔 글. 결과에 그대로 돌려준다


@dataclass(frozen=True)
class Placement:
    """검사를 통과한 AI 배치 결과. 모든 재료가 sections 나 unplaced 중 한 곳에만 있다."""
    sections: dict[str, int]        # 재료 번호 → 필드 위치 (양식 필드의 0부터 순서)
    texts: dict[str, str]           # 재료 번호 → AI 가 다듬은 글. AI 가 다듬는 재료만 쓰고, 고정 재료의 글은 무시한다
    unplaced: tuple[str, ...]       # 어느 칸에도 넣지 못한 재료 번호
