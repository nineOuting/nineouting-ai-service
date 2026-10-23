"""회의 끝 요약 → 재료 목록. 명세 7.1·7.2

요약 형식에 기대는 곳은 이 파일뿐이다. 형식(docs/api/meeting-summary-proposal.md)이 바뀌면 여기만 고친다.
입력은 요청 검사(request.py)를 통과한 값이라고 본다.

  decision        DECIDED 안건의 판정 이유      AI 가 결정 문장으로 다듬는다. 회의 끝 요약은 결정 문장을 따로 만들지 않는다
  undecided       UNDECIDED 안건                글: {결정할 사항}
  decisionCheck   DECISION_CHECK 안건           글: {결정할 사항} (결정 확인 필요: {판정 이유})
  summary         요약 문장 하나                AI 가 다듬는다 (사람이 고친 문장은 그대로)
  opinion         의견 하나                     AI 가 다듬는다 (사람이 고친 의견은 그대로)
  todo            할 일 하나                    글: {할 일} ({담당자}, {기한}[, 할 일 확인 필요])

STT 불확실 표시(「」 인용, "STT 불확실", "…로 읽힘")가 있는 글은 AI 가 다듬지 않고 원래 글 그대로 쓴다.
요약 단계(summary/agenda_*.py)가 일부러 남긴 표시인데, AI 가 다듬으면서 지우거나 한쪽으로 해석해 확정한 일이 있었다
(실제 회의 요약 시험, 2026-10-23: 「세개」(3개로 읽힘) → "3개", 「일본」(STT 불확실) 대신 → 빠짐).
"""
from .models import KIND_RANK, Material

OPINION_LABELS = {"OPINION": "의견", "EVIDENCE": "근거", "CONCERN": "우려", "QUESTION": "질문"}
NO_ASSIGNEE = "담당자 미정"
NO_DUE = "기한 미정"
TODO_UNCLEAR = "할 일 확인 필요"
STT_MARKS = ("「", "STT 불확실", "로 읽힘")


def has_stt_mark(text: str) -> bool:
    return any(mark in text for mark in STT_MARKS)


def _keep_if_marked(item: dict, text: str) -> str | None:
    """사람이 고쳤거나 STT 불확실 표시가 있으면 원래 글 그대로(고정), 아니면 None(AI 가 다듬음)."""
    return text if item.get("edited") or has_stt_mark(text) else None


def build_materials(agendas: list[dict], summary: dict) -> list[Material]:
    """요약의 안건 순서대로 재료를 만들고 m1 부터 번호를 붙인다."""
    titles = {a["agendaId"]: a for a in agendas}
    drafts = []                                     # (order, 번호 없는 재료 칸들)
    for ai, agenda in enumerate(summary.get("agendas") or []):
        info = titles[agenda["agendaId"]]
        drafts += _status_items(ai, agenda, info)
        for i, item in enumerate(agenda.get("summary") or []):
            drafts.append(_item(ai, "summary", i, item, "요약", item["text"],
                                fixed=_keep_if_marked(item, item["text"])))
        for i, item in enumerate(agenda.get("opinions") or []):
            drafts.append(_item(ai, "opinion", i, item, OPINION_LABELS.get(item.get("kind"), "의견"), item["text"],
                                fixed=_keep_if_marked(item, item["text"])))
        for i, item in enumerate(agenda.get("todos") or []):
            text = todo_text(item)
            drafts.append(_item(ai, "todo", i, item, "할 일", text, fixed=text))

    materials = []
    for n, (ai, fields) in enumerate(sorted(drafts, key=lambda d: d[1]["order"]), 1):
        agenda_id = summary["agendas"][ai]["agendaId"]
        materials.append(Material(ref=f"m{n}", agenda_id=agenda_id, agenda_title=titles[agenda_id]["title"],
                                  **fields))
    return materials


def todo_text(todo: dict) -> str:
    assignee = (todo.get("assigneeName") or "").strip() or NO_ASSIGNEE
    due = (todo.get("dueText") or "").strip() or NO_DUE
    unclear = f", {TODO_UNCLEAR}" if todo.get("unclear") else ""
    return f"{todo['task']} ({assignee}, {due}{unclear})"


def _status_items(ai, agenda, info):
    """안건 상태에서 나오는 줄: 결정, 미결, 결정 확인 필요. 그 밖의 상태는 줄이 없다.
    상태 줄은 요약 항목이 아니라 id 가 없고, 근거는 판정 이유의 줄이다."""
    status = agenda.get("status")
    reason = agenda.get("statusReason") or {}
    why = (reason.get("text") or "").strip()
    status_line = {"id": None, "evidenceSeqs": reason.get("evidenceSeqs") or [], "edited": False, "needsCheck": False}
    if status == "DECIDED":
        return [_item(ai, "decision", 0, status_line, "결정", why, fixed=_keep_if_marked({}, why))] if why else []
    if status not in ("UNDECIDED", "DECISION_CHECK"):
        return []
    # 결정할 사항이 없으면 글을 비워, 줄에는 안건 제목만 남게 한다 (lines.line_text). 제목이 두 번 나오지 않게
    point = (info.get("decisionPoint") or "").strip()
    if status == "UNDECIDED":
        kind, label, text = "undecided", "미결", point
    else:
        kind, label = "decisionCheck", "결정 확인 필요"
        mark = f"(결정 확인 필요: {why})" if why else "(결정 확인 필요)"
        text = f"{point} {mark}" if point else mark
    return [_item(ai, kind, 0, status_line, label, text, fixed=text)]


def _item(ai, kind, index, item, label, text, fixed):
    return ai, {
        "kind": kind,
        "item_id": item.get("id"),
        "label": label,
        "text": fixed if fixed is not None else text,
        "fixed_text": fixed,
        "evidence_seqs": tuple(item.get("evidenceSeqs") or ()),
        "edited": bool(item.get("edited")),
        "needs_check": bool(item.get("needsCheck")),
        "order": (ai, KIND_RANK[kind], index),
    }
