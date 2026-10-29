"""배치 결과 → 결과 콜백의 sections·unplaced. 명세 4.4~4.6, 7.2, 7.4

  줄의 글     코드가 정한 재료의 글. 앞에 "{안건 제목}: " 를 붙인다
  줄의 순서   AI 가 돌려준 순서가 아니라 재료 순서 (안건 → 결정·상태 → 요약 → 의견 → 할 일)
  근거·표시   재료의 값을 그대로 옮긴다. AI 는 쓰지 않는다
  content     줄 글에 "- " 를 붙여 줄바꿈으로 잇는다. 줄이 없으면 "해당 내용 없음"

다시 배치 (명세 8장)
  줄의 글     이전 줄 글을 그대로 쓰는 재료(whole_line)는 제목을 다시 붙이지 않는다
  줄의 순서   재료 순서 = 이전 초안의 줄 순서
  표시        draftEdited 를 더한다 (들어온 값 그대로). 작성자가 새로 쓴 줄은 sources 가 빈 목록
  넣지 못함   이전 줄에서 온 재료는 unplaced 가 아니라 unplacedLines 로, 받은 줄 그대로 돌려준다

양식 필드 (명세 v0.7, 5.4)
  사용자가 채우는 필드   받은 글 그대로, evidence 는 빈 목록. AI 는 이 필드를 고르지 않는다
  필드 모양              v0.7 요청이면 title·type·aiFilled·content·evidence. v0.6 요청(items)이면 지금처럼 title·content·evidence
"""
from .models import Field, Material, Placement

EMPTY_SECTION = "해당 내용 없음"


def build_sections(fields: list[Field | str], materials: list[Material], placement: Placement,
                   v07: bool = False) -> tuple[list[dict], list[dict]]:
    """(sections, unplaced). sections 는 양식 필드의 순서·이름 그대로다. 이름(str)만 오면 글·AI 채움 필드로 본다."""
    fields = [f if isinstance(f, Field) else Field(f) for f in fields]
    grouped: list[list[Material]] = [[] for _ in fields]
    for m in sorted(materials, key=lambda m: m.order):
        if m.ref in placement.sections:
            grouped[placement.sections[m.ref]].append(m)
    sections = []
    for field, ms in zip(fields, grouped):
        if field.ai_fill:
            evidence = [line_of(m) for m in ms]
            content = content_of(evidence)
        else:
            evidence, content = [], field.content
        section = {"title": field.title}
        if v07:
            section.update(type=field.type, aiFilled=field.ai_fill)
        sections.append({**section, "content": content, "evidence": evidence})
    unplaced_refs = set(placement.unplaced)
    unplaced = [m.source() for m in sorted(materials, key=lambda m: m.order)
                if m.ref in unplaced_refs and m.previous is None]
    return sections, unplaced


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
    line = {
        "sentence": line_text(m),
        "transcriptSeqs": list(m.evidence_seqs),
        "edited": m.edited,
        "needsCheck": m.needs_check,
        "sources": m.sources(),
    }
    if m.previous is not None:
        line["draftEdited"] = m.draft_edited
    return line


def content_of(lines: list[dict]) -> str:
    if not lines:
        return EMPTY_SECTION
    return "\n".join(f"- {line['sentence']}" for line in lines)
