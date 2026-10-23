"""배치 결과 → 결과 콜백의 sections·unplaced. 명세 4.4~4.6, 7.2, 7.4

  줄의 글     고정 재료는 코드가 정한 글, 나머지는 AI 가 다듬은 글. 앞에 "{안건 제목}: " 를 붙인다
  줄의 순서   AI 가 돌려준 순서가 아니라 재료 순서 (안건 → 결정·상태 → 요약 → 의견 → 할 일)
  근거·표시   재료의 값을 그대로 옮긴다. AI 는 쓰지 않는다
  content     줄 글에 "- " 를 붙여 줄바꿈으로 잇는다. 줄이 없으면 "해당 내용 없음"
"""
from .models import Material, Placement

EMPTY_SECTION = "해당 내용 없음"


def build_sections(items: list[str], materials: list[Material], placement: Placement) -> tuple[list[dict], list[dict]]:
    """(sections, unplaced). sections 는 items 의 순서·이름 그대로다."""
    grouped: list[list[Material]] = [[] for _ in items]
    for m in sorted(materials, key=lambda m: m.order):
        if m.ref in placement.sections:
            grouped[placement.sections[m.ref]].append(m)
    sections = []
    for title, ms in zip(items, grouped):
        lines = [line_of(m, placement) for m in ms]
        sections.append({"title": title, "content": content_of(lines), "evidence": lines})
    unplaced_refs = set(placement.unplaced)
    unplaced = [m.source() for m in sorted(materials, key=lambda m: m.order) if m.ref in unplaced_refs]
    return sections, unplaced


def line_text(m: Material, placement: Placement) -> str:
    """글이 비면(결정할 사항이 없는 미결 안건) 안건 제목만 쓴다."""
    body = m.fixed_text if m.fixed_text is not None else placement.texts[m.ref]
    return f"{m.agenda_title}: {body}" if body else m.agenda_title


def line_of(m: Material, placement: Placement) -> dict:
    return {
        "sentence": line_text(m, placement),
        "transcriptSeqs": list(m.evidence_seqs),
        "edited": m.edited,
        "needsCheck": m.needs_check,
        "sources": [m.source()],
    }


def content_of(lines: list[dict]) -> str:
    if not lines:
        return EMPTY_SECTION
    return "\n".join(f"- {line['sentence']}" for line in lines)
