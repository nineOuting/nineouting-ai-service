"""회의록 초안의 자료 모양. 명세: docs/api/minutes-draft-contract.md"""
from dataclasses import dataclass

# 재료 종류. 칸 안의 줄 순서도 이 묶음 순서를 따른다 (결정할 사항 줄 → 요약 → 의견 → 할 일)
KIND_RANK = {"decision": 0, "undecided": 0, "decisionCheck": 0, "summary": 1, "opinion": 2, "todo": 3}


@dataclass(frozen=True)
class Material:
    """재료 항목 하나. 초안의 줄 하나가 된다.

    글(text)은 코드가 정한다. AI 는 칸만 고른다 (명세 v0.8: 결정 줄도 decisionText 그대로).
    """
    ref: str                        # AI 입력에서 가리키는 번호 "m1", "m2" …
    agenda_id: int | None           # 작성자가 새로 쓴 줄은 None
    agenda_title: str
    kind: str                       # KIND_RANK 의 키
    item_id: str | None             # 요약 항목 id. 결정할 사항에서 나온 줄(decision, undecided, decisionCheck)은 None
    label: str                      # AI 입력에 보여 줄 종류 이름 (결정, 미결, 우려 …)
    text: str                       # 줄의 글 ("{안건 제목}: " 앞까지). AI 입력에도 이 글을 보여 준다
    evidence_seqs: tuple[int, ...]
    edited: bool
    needs_check: bool
    order: tuple[int, int, int]     # (안건 순서, 종류 순서, 같은 종류 안 순서). 칸 안 줄 순서에 쓴다
    decision_point_id: int | str | None = None      # 결정할 사항에서 나온 줄의 사항 번호 (명세 5.6)
    # 표 필드의 칸 값이 될 수 있는 글 (명세 7.6). (이름, 글) 목록. 값이 없는 조각은 넣지 않는다
    parts: tuple[tuple[str, str], ...] = ()
    # 다시 배치 (명세 8장, relayout.py). 처음 초안에서는 기본값 그대로다
    previous: int | None = None     # 이 재료가 된 previousLines 의 위치
    draft_edited: bool = False      # 그 줄의 draftEdited 그대로
    whole_line: bool = False        # text 가 "{안건 제목}: " 까지 붙은 줄 글 전체다 (이전 줄 글을 그대로 쓸 때)
    sourced: bool = True            # False 면 출처가 없는 줄 (작성자가 새로 쓴 줄). 결과의 sources 가 빈 목록

    def source(self) -> dict:
        """결과의 sources·unplaced 항목."""
        return {"agendaId": self.agenda_id, "kind": self.kind, "id": self.item_id,
                "decisionPointId": self.decision_point_id}

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
    description: str = ""           # 양식 편집 화면의 필드 설명. AI 가 칸을 고를 때 이름과 함께 본다 (명세 7.2)
    columns: int | None = None      # 표 필드의 열 개수 (명세 5.8). 없으면 AI 가 1~6개로 정한다


@dataclass(frozen=True)
class Table:
    """검사를 통과한 표 채우기 결과 (명세 7.6). 놓인 재료마다 행이 하나씩 있다."""
    columns: tuple[str, ...]
    rows: dict[str, tuple[str, ...]]  # 재료 번호 → 칸 글 (columns 와 같은 순서·개수)


@dataclass(frozen=True)
class Placement:
    """검사를 통과한 AI 배치 결과. 모든 재료가 sections 나 unplaced 중 한 곳에만 있다."""
    sections: dict[str, int]        # 재료 번호 → 필드 위치 (양식 필드의 0부터 순서)
    unplaced: tuple[str, ...]       # 어느 칸에도 넣지 못한 재료 번호
