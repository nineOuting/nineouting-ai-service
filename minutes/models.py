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
    agenda_id: int
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

    @property
    def rewrite(self) -> bool:
        """AI 가 글을 다듬어야 하는 재료인가."""
        return self.fixed_text is None

    def source(self) -> dict:
        """결과의 sources·unplaced 항목."""
        return {"agendaId": self.agenda_id, "kind": self.kind, "id": self.item_id}
