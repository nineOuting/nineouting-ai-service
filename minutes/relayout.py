"""양식 변경 시 다시 배치: 이전 초안의 줄(previousLines) → 재료 목록. 명세 8장

이전 줄 하나가 재료 하나가 된다. 줄마다 재료 번호(m1, m2 …)를 따로 붙이므로 출처가 겹쳐도 검사에 걸리지 않는다.

  고치지 않은 줄 (draftEdited false)   출처로 요약 항목을 찾아 처음 초안과 같은 재료로 다시 만든다
  작성자가 고친 줄                     글 고정. 근거·표시·출처는 그 줄의 값 그대로. 표 칸 값의 조각은 안건·종류·줄 글
  작성자가 새로 쓴 줄 (출처 없음)      글 고정. 출처 없음
  출처 항목이 요약에 없는 줄           고친 줄처럼 (8.3)
  출처가 겹치는 줄의 두 번째부터       고친 줄처럼 (8.3). 고치지 않은 줄이 있으면 그 줄이 첫 줄

출처는 (안건, 종류, 항목 id, 결정할 사항 번호)로 찾는다. 사항의 상태가 바뀌어 종류가 달라졌거나(결정 확인 필요 → 결정)
v0.7 결과로 저장해 사항 번호가 없는 상태 줄은 찾지 못하므로, 출처 항목이 요약에 없는 줄로 다룬다 (명세 8.3).
이전 줄의 글은 이미 "{안건 제목}: " 이 붙은 줄 글이라, 고정한 글에는 제목을 다시 붙이지 않는다 (whole_line).
이전 초안에 없던 요약 항목(처음에 넣지 못했거나 작성자가 지운 것)은 AI 에 보내지 않고 unplaced 로 돌려준다.
칸 안의 줄 순서는 이전 초안의 줄 순서를 따른다 (작성자가 바꾼 순서를 지키고, 새로 쓴 줄은 안건이 없어 끼울 자리가 없으므로).
"""
import re
from dataclasses import replace

from .materials import build_materials
from .models import Material

WRITTEN = "written"                     # 작성자가 새로 쓴 줄의 재료 종류. 결과에는 나가지 않는다 (sources 가 빈 목록)
WRITTEN_LABEL = "직접 작성"
KIND_LABELS = {"decision": "결정", "undecided": "미결", "decisionCheck": "결정 확인 필요", "summary": "요약",
               "opinion": "의견", "todo": "할 일"}


def build_relayout(agendas: list[dict], summary: dict, previous_lines: list[dict]) -> tuple[list[Material], list[Material]]:
    """(AI 에 보낼 재료, 이전 초안에 없던 요약 재료). 앞의 것은 previousLines 순서, 번호는 m1 부터."""
    by_source = {_key(m.source()): m for m in build_materials(agendas, summary)}
    titles = {a["agendaId"]: a["title"] for a in agendas}

    # 출처대로 다시 만드는 줄: 출처마다 고치지 않은 첫 줄 하나 (8.3)
    rebuild = {}
    for i, line in enumerate(previous_lines):
        key = _first_source_key(line)
        if not line["draftEdited"] and key in by_source and key not in rebuild:
            rebuild[key] = i

    materials, referenced = [], set()
    for i, line in enumerate(previous_lines):
        key = _first_source_key(line)
        if key is not None:
            referenced.add(key)
        ref = f"m{i + 1}"
        if key is not None and rebuild.get(key) == i:
            materials.append(_rebuilt(by_source[key], ref, i))
        else:
            materials.append(_kept(line, ref, i, by_source.get(key), titles))
    unreferenced = [m for k, m in by_source.items() if k not in referenced]
    return materials, unreferenced


def previous_lines_from(result: dict) -> list[dict]:
    """처음 초안 결과(SUCCEEDED) → 다시 배치 요청의 previousLines. 백엔드가 줄을 저장했다가 돌려보내는 모양 (명세 5.7).
    작성자가 아무것도 고치지 않은 상태다 (draftEdited false). 손 시험 도구와 시험에서 쓴다."""
    return [{"title": s["title"], **line, "draftEdited": False} for s in result["sections"] for line in s["evidence"]]


def flatten(text: str) -> str:
    """고정한 글의 줄바꿈을 공백으로 (명세 8.3, 7.3 빈 줄 검사)."""
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _rebuilt(m: Material, ref: str, i: int) -> Material:
    return replace(m, ref=ref, order=(i, 0, 0), previous=i)


def _kept(line: dict, ref: str, i: int, original: Material | None, titles: dict) -> Material:
    """글을 고정한 줄. 근거·표시·출처는 줄의 값 그대로."""
    text = flatten(line["sentence"])
    sources = line.get("sources") or []
    if sources:
        src = sources[0]
        agenda_id, kind, item_id, point_id = src["agendaId"], src["kind"], src.get("id"), src.get("decisionPointId")
        title = original.agenda_title if original else titles.get(agenda_id, "")
        label = original.label if original else KIND_LABELS[kind]
    else:
        agenda_id, kind, item_id, point_id, title, label = None, WRITTEN, None, None, "", WRITTEN_LABEL
    parts = ((("안건", title),) if title else ()) + (("종류", label), ("줄 글", text))     # 표 칸 값 (명세 7.6)
    return Material(ref=ref, agenda_id=agenda_id, agenda_title=title, kind=kind, item_id=item_id, label=label,
                    text=text, parts=parts, evidence_seqs=tuple(line.get("transcriptSeqs") or ()),
                    edited=bool(line.get("edited")), needs_check=bool(line.get("needsCheck")), order=(i, 0, 0),
                    decision_point_id=point_id, previous=i, draft_edited=line["draftEdited"], whole_line=True, sourced=bool(sources))


def _first_source_key(line: dict):
    sources = line.get("sources") or []
    return _key(sources[0]) if sources else None


def _key(source: dict) -> tuple:
    return source["agendaId"], source["kind"], source.get("id"), source.get("decisionPointId")
