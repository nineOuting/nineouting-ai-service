"""배치 결과 → 결과 콜백의 sections·unplaced. 명세 4.4~4.6, 7.2, 7.4

  줄의 글     코드가 정한 재료의 글. 앞에 "{안건 제목}: " 를 붙인다
  줄의 순서   AI 가 돌려준 순서가 아니라 재료 순서 (안건 → 결정·상태 → 요약 → 의견 → 할 일)
  근거·표시   재료의 값을 그대로 옮긴다. AI 는 쓰지 않는다
  content     마크다운 목록 (명세 5.4). 줄이 없으면 "해당 내용 없음". 기계가 읽을 값은 evidence 다
              묶는 기준(AI 가 칸마다 고름, 명세 7.2)이 있으면 기준 값을 제목 줄로 쓰고 그 아래에 줄을 한 단계 들여 쓴다
                안건    제목 안건 제목, 줄 글은 "{안건 제목}: " 을 뗀 글
                담당자  제목 할 일의 담당자, 줄 글 그대로
                종류    제목 결정·미결·결정 확인 필요·요약·의견·할 일, 줄 글 그대로
              묶음 순서는 처음 나온 순서, 묶음 안은 줄 순서. 기준 값이 없는 줄(작성자가 새로 쓴 줄, 담당자 없는 할 일 등)은
              묶음 뒤에 들여 쓰지 않고 둔다. 기준 값이 1개 이하이면 묶지 않는다.
              단, 줄이 2개 이상이고 모두 한 안건이면 기준과 상관없이 안건 제목 아래로 묶는다. 묶지 않으면 줄마다
              같은 안건 제목이 되풀이된다 (실제 회의: 한 안건만 든 필드에서 줄 4개가 모두 같은 제목으로 시작함).
              담당자 기준은 담당자가 1명 이하이면 안건으로 묶는다. 담당자를 못 적은 회의가 많고, 그때 AI 가 어느 기준을
              골랐는지에 따라 같은 내용의 모양이 달라졌다 (SCRUM-180)
              묶지 않을 때는 줄 글에 "- " 를 붙여 줄바꿈으로 잇는다

다시 배치 (명세 8장)
  줄의 글     이전 줄 글을 그대로 쓰는 재료(whole_line)는 제목을 다시 붙이지 않는다
  줄의 순서   재료 순서 = 이전 초안의 줄 순서
  표시        draftEdited 를 더한다 (들어온 값 그대로). 작성자가 새로 쓴 줄은 sources 가 빈 목록
  넣지 못함   이전 줄에서 온 재료는 unplaced 가 아니라 unplacedLines 로, 받은 줄 그대로 돌려준다

양식 필드 (명세 v0.7, 5.4)
  사용자가 채우는 필드   받은 글 그대로, evidence 는 빈 목록. AI 는 이 필드를 고르지 않는다
  필드 모양              v0.7 요청이면 title·type·aiFilled·content·evidence. v0.6 요청(items)이면 지금처럼 title·content·evidence

AI 가 채우는 표 필드 (명세 7.6, 5.9)
  열·칸       표 채우기(table.py)가 정한 값. 필드 모양에 columns 가 더해진다
  행          줄과 같은 순서·근거·표시·출처에 sentence 대신 cells. content 는 마크다운 표
  빈 표       놓인 재료가 없으면 columns []·"해당 내용 없음"·evidence []
"""
from .models import BY_AGENDA, BY_ASSIGNEE, BY_KIND, KIND_NAMES, NO_GROUPING, TABLE, Field, Material, Placement, Table
from .table import markdown

EMPTY_SECTION = "해당 내용 없음"


def build_sections(fields: list[Field | str], materials: list[Material], placement: Placement,
                   v07: bool = False, tables: dict[int, Table] | None = None) -> tuple[list[dict], list[dict]]:
    """(sections, unplaced). sections 는 양식 필드의 순서·이름 그대로다. 이름(str)만 오면 글·AI 채움 필드로 본다.
    tables 는 필드 위치 → 표 채우기 결과. 놓인 재료가 있는 AI 채움 표 필드마다 있어야 한다."""
    fields = [f if isinstance(f, Field) else Field(f) for f in fields]
    tables = tables or {}
    sections = []
    for i, (field, ms) in enumerate(zip(fields, group(len(fields), materials, placement))):
        section = {"title": field.title}
        if v07:
            section.update(type=field.type, aiFilled=field.ai_fill)
        if not field.ai_fill:
            section.update(content=field.content, evidence=[])
        elif field.type == TABLE:
            table = tables.get(i)
            rows = [row_of(m, table.rows[m.ref]) for m in ms] if ms else []
            section.update(columns=list(table.columns) if ms else [],
                           content=markdown(table, [r["cells"] for r in rows]) if ms else EMPTY_SECTION,
                           evidence=rows)
        else:
            section.update(content=content_of(ms, placement.grouping.get(i, NO_GROUPING)),
                           evidence=[line_of(m) for m in ms])
        sections.append(section)
    unplaced_refs = set(placement.unplaced)
    unplaced = [m.source() for m in sorted(materials, key=lambda m: m.order)
                if m.ref in unplaced_refs and m.previous is None]
    return sections, unplaced


def group(n_fields: int, materials: list[Material], placement: Placement) -> list[list[Material]]:
    """필드 위치마다 놓인 재료. 순서는 AI 가 돌려준 순서가 아니라 재료 순서."""
    grouped: list[list[Material]] = [[] for _ in range(n_fields)]
    for m in sorted(materials, key=lambda m: m.order):
        if m.ref in placement.sections:
            grouped[placement.sections[m.ref]].append(m)
    return grouped


def unplaced_lines(materials: list[Material], placement: Placement, previous_lines: list[dict]) -> list[dict]:
    """AI 가 어느 칸에도 넣지 못한 이전 줄. 받은 줄 그대로, 이전 초안의 순서로."""
    unplaced_refs = set(placement.unplaced)
    return [previous_lines[m.previous] for m in sorted(materials, key=lambda m: m.order)
            if m.ref in unplaced_refs and m.previous is not None]


def line_text(m: Material) -> str:
    if m.whole_line:
        return m.text
    return f"{m.agenda_title}: {m.text}"


def line_of(m: Material) -> dict:
    return {"sentence": line_text(m), **_trace(m)}


def row_of(m: Material, cells: tuple[str, ...]) -> dict:
    """표 필드의 행 하나 (명세 5.9). 근거·표시·출처는 줄과 같다."""
    return {"cells": list(cells), **_trace(m)}


def _trace(m: Material) -> dict:
    out = {"transcriptSeqs": list(m.evidence_seqs), "edited": m.edited, "needsCheck": m.needs_check,
           "sources": m.sources()}
    if m.previous is not None:
        out["draftEdited"] = m.draft_edited
    return out


def content_of(materials: list[Material], by: str = NO_GROUPING) -> str:
    """글 필드 하나의 글. materials 는 그 필드에 놓인 재료를 재료 순서로."""
    if not materials:
        return EMPTY_SECTION
    keys = [group_key(m, by) for m in materials]
    if by != BY_AGENDA and len({k for k in keys if k is not None}) <= 1:
        agendas = [group_key(m, BY_AGENDA) for m in materials]
        if by == BY_ASSIGNEE or (None not in agendas and len(set(agendas)) == 1):
            by, keys = BY_AGENDA, agendas
    keyed = [k for k in keys if k is not None]
    if len(set(keyed)) <= 1 and (by != BY_AGENDA or len(keyed) <= 1):
        return "\n".join(f"- {line_text(m)}" for m in materials)
    groups: dict[str, list[str]] = {}
    loose = []
    for m, key in zip(materials, keys):
        if key is None:
            loose.append(line_text(m))
        else:
            groups.setdefault(key, []).append(_child_text(m, by))
    out = []
    for key, texts in groups.items():
        out.append(f"- {key}")
        out += [f"  - {t}" for t in texts]
    out += [f"- {t}" for t in loose]
    return "\n".join(out)


def group_key(m: Material, by: str) -> str | None:
    """재료가 들어갈 묶음의 제목. 없으면 None (묶음 뒤에 둔다)."""
    if by == BY_AGENDA and m.agenda_id is not None:
        key = m.agenda_title
    elif by == BY_ASSIGNEE and m.kind == "todo":
        key = dict(m.parts).get("담당자")
    elif by == BY_KIND:
        key = KIND_NAMES.get(m.kind)
    else:
        return None
    key = " ".join((key or "").split())      # 제목 줄이 한 줄이 되게
    return key or None


def _child_text(m: Material, by: str) -> str:
    """제목 줄 아래에 쓸 글. 안건으로 묶으면 안건 제목을 뗀다 (이전 줄 글은 앞의 "{안건 제목}: " 이 있을 때만)."""
    if by != BY_AGENDA:
        return line_text(m)
    if not m.whole_line:
        return m.text
    prefix = f"{m.agenda_title}: "
    return m.text[len(prefix):] if m.text.startswith(prefix) else m.text
