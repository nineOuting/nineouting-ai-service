"""진행 중 회의를 챗봇 근거로 바꾼다 (SCRUM-224, 명세 docs/api/rag-chat-contract.md 4.2).

  안건 보드(liveAgendas)   결정할 사항·요약 문장·의견·할 일 하나가 근거 하나
  최근 전사 줄(recentLines) 맞장구·간투사·확신도 낮은 줄을 빼고 5줄 또는 400자씩 묶은 것이 근거 하나
  실시간 요약(liveSummary)  요약 문장 하나가 근거 하나 (SCRUM-249, 명세 v0.5 3.6). 안건 보드에 없는 안건 밖 이야기와
                           최근 전사 줄보다 오래된 이야기를 찾는 데 쓴다

넘기는 양: 출처마다 글 상한(보드 6,000자, 전사 줄 4,000자) 안이면 모두 넘긴다. 넘으면 임베딩 없이 정해 둔 순서로 고른다.
  보드    ① 지금 안건의 모든 항목 ② 다른 안건의 결정할 사항 ③ 할 일 ④ 요약 ⑤ 의견
  전사 줄  가장 최근 묶음부터
  실시간 요약 (6,000자) 질문과 가까운 항목(임베딩)부터. 최근 것부터 넣으면 초반 이야기가 다시 빠지기 때문
상한과 묶음 크기는 정해 둔 값이며, 긴 합성 회의로 재고 고친다. 질문과 가까운 순서(임베딩)는 넘친 탓에 틀리는 사례가
나오면 그때 더한다 (2026-11-12 사용자 결정).

근거 글(Evidence.text)은 응답의 citations[].text 로도 나가므로 보드·전사 글 그대로 둔다.
"지금 안건", "질문한 사용자의 할 일" 같은 표시는 AI 에만 넘기는 머리(Evidence.tags)에 둔다.
여기서는 로그를 남기지 않는다 (글을 다루므로).
"""
from __future__ import annotations

from dataclasses import dataclass

LIVE = "live"
TITLE = "진행 중 회의"
WHEN = "지금 진행 중"                     # 지난 회의록의 "회의 2026년 …" 자리에 붙는다. 가장 최근이라는 뜻
BOARD_MAX_CHARS = 6000                   # 정해 둔 값
LINES_MAX_CHARS = 4000                   # 정해 둔 값
BUNDLE_LINES = 5                         # 정해 둔 값
BUNDLE_CHARS = 400                       # 정해 둔 값
SUMMARY_MAX_CHARS = 6000                 # 정해 둔 값. 33분 시험 회의의 요약이 5,945자 (2026-11-13 측정)
SUMMARY_EMBED_CHARS = 300                # 가까운 순서를 매길 때 임베딩하는 글 앞부분 (모델 입력 상한 512토큰 안). 항목 평균 132자 (측정)

TAG_CURRENT = "지금 안건"
TAG_MINE = "질문한 사용자의 할 일"
TAG_UNCLEAR = "할 일인지 확인 필요"
TAG_SUMMARY = "실시간 요약"

DECISION_KIND = {"DECIDED": "결정", "DECISION_CHECK": "결정 확인 필요", "UNDECIDED": "미결"}   # NOT_STARTED 는 넘기지 않음
OPINION_KIND = {"OPINION": "의견", "EVIDENCE": "의견의 근거", "CONCERN": "우려", "QUESTION": "질문"}
ITEM_NAME = {"decision": "결정", "summary": "요약", "opinion": "의견", "todo": "할 일"}

# 넘칠 때 고르는 순서. 0 은 지금 안건 (항목 종류와 관계없이 먼저)
_RANK = {"decision": 1, "todo": 2, "summary": 3, "opinion": 4}
# 넘기는 순서: 안건 순서대로, 한 안건 안에서는 이 순서
_SHOW = {"decision": 0, "summary": 1, "opinion": 2, "todo": 3}


@dataclass(frozen=True)
class LiveItem:
    text: str
    location: dict
    tags: tuple[str, ...]
    rank: int                            # 넘칠 때 고르는 순서 (작을수록 먼저)
    order: tuple                         # 넘기는 순서


def _same_id(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def _location(agenda, item, item_id, seqs) -> dict:
    title = agenda.get("title") or ""
    return {"type": "agenda", "agendaId": agenda.get("agendaId"), "item": item, "itemId": item_id,
            "evidenceSeqs": list(seqs or []), "label": f"{title} 안건의 {ITEM_NAME[item]}"}


def _todo_detail(t) -> str:
    parts = [f"담당: {t['assigneeName']}" if t.get("assigneeName") else "담당: 정하지 않음"]
    if t.get("dueText"):
        parts.append(f"기한: {t['dueText']}")
    return ", ".join(parts)


def board_items(agendas, current_agenda_id=None, user_id=None) -> list[LiveItem]:
    """안건 보드 → 항목 목록 (넘기는 순서). 글이 빈 항목, 논의 전·알 수 없는 상태의 결정할 사항은 뺀다."""
    items = []
    # 지금 안건을 맨 앞에 둔다. 나머지는 받은 순서 (백엔드가 안건 순서로 담는다, 명세 3.1)
    ordered = sorted(enumerate(agendas), key=lambda p: (not _same_id(p[1].get("agendaId"), current_agenda_id), p[0]))
    for pos, (_, a) in enumerate(ordered):
        title = a.get("title") or ""
        current = _same_id(a.get("agendaId"), current_agenda_id)
        base_tags = (TAG_CURRENT,) if current else ()

        def add(item, kind_name, body, item_id, seqs, n, extra=()):
            body = (body or "").strip()
            if not body:
                return
            items.append(LiveItem(f"{title} / {kind_name}: {body}", _location(a, item, item_id, seqs),
                                  base_tags + tuple(extra), 0 if current else _RANK[item], (pos, _SHOW[item], n)))

        for n, d in enumerate(a.get("decisionPoints") or []):
            kind = DECISION_KIND.get(d.get("status"))
            if kind:
                add("decision", kind, d.get("decisionText"), d.get("decisionPointId"), d.get("evidenceSeqs"), n)
        for n, s in enumerate(a.get("summary") or []):
            add("summary", "요약", s.get("text"), s.get("id"), s.get("evidenceSeqs"), n)
        for n, o in enumerate(a.get("opinions") or []):
            add("opinion", OPINION_KIND.get(o.get("kind"), "의견"), o.get("text"), o.get("id"), o.get("evidenceSeqs"), n)
        for n, t in enumerate(a.get("todos") or []):
            extra = [TAG_MINE] if _same_id(t.get("assigneeUserId"), user_id) else []
            if t.get("unclear"):
                extra.append(TAG_UNCLEAR)
            task = (t.get("task") or "").strip()
            add("todo", "할 일", f"{task} ({_todo_detail(t)})" if task else "", t.get("id"), t.get("evidenceSeqs"),
                n, extra)
    return items


def _clock(ms) -> str:
    s = max(int(ms or 0), 0) // 1000
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def line_bundles(lines, max_lines=BUNDLE_LINES, max_chars=BUNDLE_CHARS) -> list[LiveItem]:
    """전사 줄 → 묶음 (시간 순서). kind 가 있는 줄(맞장구·간투사)과 lowConfidence 줄, 빈 줄은 뺀다.
    한 묶음은 max_lines 줄 이하이고, 줄을 더하면 max_chars 자를 넘을 때 새 묶음을 연다 (한 줄이 넘으면 그 줄만 한 묶음)."""
    kept = [ln for ln in lines if not ln.get("kind") and not ln.get("lowConfidence") and (ln.get("text") or "").strip()]
    groups, cur, size = [], [], 0
    for ln in kept:
        n = len(ln["text"].strip())
        if cur and (len(cur) >= max_lines or size + n > max_chars):
            groups.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += n
    if cur:
        groups.append(cur)
    out = []
    for i, g in enumerate(groups):
        first, last = g[0], g[-1]
        end = last.get("endMs") if last.get("endMs") is not None else last.get("startMs")
        loc = {"type": "time", "seq": first["seq"], "startMs": first.get("startMs"), "endMs": end,
               "label": _clock(first.get("startMs"))}
        out.append(LiveItem("\n".join(ln["text"].strip() for ln in g), loc, (), 0, (i,)))
    return out


def pick_board(items: list[LiveItem], max_chars=BOARD_MAX_CHARS) -> list[LiveItem]:
    """상한 안이면 모두. 넘으면 rank 순서로 넣을 수 있는 것을 넣는다 (넘는 항목은 건너뛰고 다음 것을 본다). 넘기는 순서는 유지."""
    if sum(len(i.text) for i in items) <= max_chars:
        return items
    chosen, used = set(), 0
    for idx in sorted(range(len(items)), key=lambda k: (items[k].rank, items[k].order)):
        n = len(items[idx].text)
        if used + n <= max_chars:
            chosen.add(idx)
            used += n
    return [it for k, it in enumerate(items) if k in chosen]


def pick_lines(bundles: list[LiveItem], max_chars=LINES_MAX_CHARS) -> list[LiveItem]:
    """가장 최근 묶음부터 상한까지. 넘기는 순서는 시간 순서. 상한보다 큰 묶음 하나는 건너뛰지 않고 멈춘다
    (그보다 오래된 묶음을 넣으면 가운데가 비기 때문)."""
    chosen, used = [], 0
    for b in reversed(bundles):
        if used + len(b.text) > max_chars:
            break
        chosen.append(b)
        used += len(b.text)
    return list(reversed(chosen))


def _summary_span(evidence):
    """요약 항목의 근거 구간 (초) → (startMs, endMs). 첫 start 와 마지막 end. 없으면 (None, None)."""
    spans = [e for e in (evidence or []) if isinstance(e, dict)]
    if not spans:
        return None, None
    return int(round(spans[0]["start"] * 1000)), int(round(spans[-1]["end"] * 1000))


def summary_items(summary) -> list[LiveItem]:
    """실시간 요약 → 항목 목록 (받은 순서 = 시간 순서). 글이 빈 항목은 뺀다.
    위치는 time (seq 없음). 주제는 AI 에만 넘기는 표시(tags)에 둔다. citations[].text 는 요약 글 그대로."""
    out = []
    for i, it in enumerate(summary):
        text = (it.get("text") or "").strip()
        if not text:
            continue
        start, end = _summary_span(it.get("evidence"))
        loc = {"type": "time", "startMs": start, "endMs": end,
               "label": _clock(start) if start is not None else TAG_SUMMARY}
        topic = (it.get("topic") or "").strip()
        tags = (TAG_SUMMARY,) + ((f"주제 {topic}",) if topic else ())
        out.append(LiveItem(text, loc, tags, 0, (i,)))
    return out


def summary_embed_text(item: LiveItem) -> str:
    """가까운 순서를 매길 때 임베딩하는 글: 주제 + 글 앞부분."""
    topic = next((t.removeprefix("주제 ") for t in item.tags if t.startswith("주제 ")), "")
    return ((f"{topic}: " if topic else "") + item.text)[:SUMMARY_EMBED_CHARS]


def pick_summary(items: list[LiveItem], query: str | None = None, embedder=None,
                 max_chars=SUMMARY_MAX_CHARS) -> list[LiveItem]:
    """상한 안이면 모두 (임베딩을 부르지 않는다). 넘으면 질문과 가까운 순서로 넣을 수 있는 것을 넣는다
    (넘는 항목은 건너뛰고 다음 것을 본다). 넘기는 순서는 시간 순서.
    embedder: embed_query·embed_passages 가 있는 것 (벡터는 길이 1). 없거나 질문이 비면 가장 최근 것부터 고른다."""
    if sum(len(i.text) for i in items) <= max_chars:
        return items
    if embedder is None or not (query or "").strip():
        order = list(reversed(range(len(items))))
    else:
        q = embedder.embed_query(query)
        vecs = embedder.embed_passages([summary_embed_text(i) for i in items])
        scores = [sum(a * b for a, b in zip(v, q)) for v in vecs]
        order = sorted(range(len(items)), key=lambda k: (-scores[k], k))
    chosen, used = set(), 0
    for k in order:
        n = len(items[k].text)
        if used + n <= max_chars:
            chosen.add(k)
            used += n
    return [it for k, it in enumerate(items) if k in chosen]
