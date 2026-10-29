"""회의 끝 요약 → 재료 목록. 명세 7.1·7.2

요약 형식에 기대는 곳은 이 파일뿐이다. 형식(안건 보드 todaySummary, docs/api/agenda-today-summary-contract.md)이 바뀌면 여기만 고친다.
입력은 요청 검사(request.py)를 통과한 값이라고 본다.

  decision        DECIDED 인 결정할 사항 하나          글: {decisionText}
  undecided       UNDECIDED 인 결정할 사항 하나        글: {사항 문구}. 짝이 맞는 문구가 없으면 {decisionText}
  decisionCheck   DECISION_CHECK 인 결정할 사항 하나   글: {사항 문구} (결정 확인 필요: {decisionText})
  summary         요약 문장 하나                       원래 글 그대로
  opinion         의견 하나                            원래 글 그대로
  todo            할 일 하나                           글: {할 일} ({담당자}, {기한}[, 할 일 확인 필요])

결정·미결 줄은 안건마다가 아니라 결정할 사항마다 만든다 (명세 v0.8). 회의 끝 요약이 결정을 사항마다 판정하고,
한 안건 안에 결정된 사항과 미결 사항이 섞일 수 있기 때문이다. 안건의 status·statusReason 은 보지 않는다
(statusReason 은 "3개 중 1개가 결정됨" 같은 개수 문장이다).

모든 글은 코드가 정하고 AI 는 칸만 고른다. 결정 내용(decisionText)은 요약 쪽이 검증까지 마친 한 줄이고,
요약 문장과 의견도 요약 단계에서 정리된 글이라 다시 다듬지 않는다. 다듬게 했을 때 얻는 것은 말끝 정도였고 잃는 것이 더 많았다
(실제 회의 요약 시험, 2026-10-23: STT 불확실 표시 삭제 「세개」(3개로 읽힘) → "3개", 내용 누락 「일본」(STT 불확실) 대신 → 빠짐,
"이야기가 나옴" → "이야기가 나왔다는 의견이 나옴" 같은 겹문장).
"""
from .models import KIND_RANK, Material

OPINION_LABELS = {"OPINION": "의견", "EVIDENCE": "근거", "CONCERN": "우려", "QUESTION": "질문"}
POINT_KINDS = {"DECIDED": ("decision", "결정"), "UNDECIDED": ("undecided", "미결"),
               "DECISION_CHECK": ("decisionCheck", "결정 확인 필요")}
NO_ASSIGNEE = "담당자 미정"
NO_DUE = "기한 미정"
TODO_UNCLEAR = "할 일 확인 필요"


def build_materials(agendas: list[dict], summary: dict) -> list[Material]:
    """요약의 안건 순서대로 재료를 만들고 m1 부터 번호를 붙인다."""
    titles = {a["agendaId"]: a for a in agendas}
    drafts = []                                     # (order, 번호 없는 재료 칸들)
    for ai, agenda in enumerate(summary.get("agendas") or []):
        info = titles[agenda["agendaId"]]
        drafts += _point_items(ai, agenda, info)
        for i, item in enumerate(agenda.get("summary") or []):
            drafts.append(_item(ai, "summary", i, item, "요약", item["text"]))
        for i, item in enumerate(agenda.get("opinions") or []):
            drafts.append(_item(ai, "opinion", i, item, OPINION_LABELS.get(item.get("kind"), "의견"), item["text"]))
        for i, item in enumerate(agenda.get("todos") or []):
            drafts.append(_item(ai, "todo", i, item, "할 일", todo_text(item)))

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


def agenda_points(agenda: dict) -> list[dict]:
    """요청 agendas[] 하나의 결정할 사항 목록 (position 순).
    전환 기간: decisionPoints 가 없고 decisionPoint 글이 있으면 번호 없는 사항 1개로 본다 (명세 5.1). 둘 다 없으면 빈 목록."""
    if "decisionPoints" in agenda:
        return sorted(agenda["decisionPoints"], key=lambda p: p["position"])
    text = (agenda.get("decisionPoint") or "").strip()
    return [{"decisionPointId": None, "position": 1, "text": text}] if text else []


def _point_items(ai, agenda, info):
    """결정할 사항마다 줄 하나: 결정, 미결, 결정 확인 필요. 그 밖의 상태(NOT_STARTED 등)는 줄이 없다.
    줄 순서는 안건 목록의 사항 순서(position)이고, 안건 목록과 짝이 맞지 않는 사항은 그 뒤에 요약의 순서로 둔다."""
    points = agenda_points(info)
    rank = {p["decisionPointId"]: i for i, p in enumerate(points)}
    texts = {p["decisionPointId"]: p["text"].strip() for p in points}
    out = []
    for j, result in enumerate(agenda.get("decisionPoints") or []):
        if result.get("status") not in POINT_KINDS:
            continue
        kind, label = POINT_KINDS[result["status"]]
        pid = result.get("decisionPointId")
        decided = result["decisionText"].strip()
        # 번호가 없는 사항(전환 기간)은 안건 목록에도 번호 없는 사항이 하나뿐일 때만 짝으로 본다
        paired = pid in texts and (pid is not None or len(points) == 1)
        point = texts[pid] if paired else ""
        if kind == "decision":
            text = decided
        elif kind == "undecided":
            text = point or decided
        else:
            mark = f"(결정 확인 필요: {decided})"
            text = f"{point} {mark}" if point else mark
        index = rank[pid] if paired else len(points) + j
        out.append(_item(ai, kind, index, result, label, text, decision_point_id=pid))   # 사항 결과에는 id 가 없다
    return out


def _item(ai, kind, index, item, label, text, decision_point_id=None):
    return ai, {
        "kind": kind,
        "item_id": item.get("id"),
        "label": label,
        "text": text,
        "evidence_seqs": tuple(item.get("evidenceSeqs") or ()),
        "edited": bool(item.get("edited")),
        "needs_check": bool(item.get("needsCheck")),
        "order": (ai, KIND_RANK[kind], index),
        "decision_point_id": decision_point_id,
    }
