"""회의록 초안 만들기 전체 흐름. 명세 2·3·6·7장

  요청 dict → 재료 → (재료가 있으면) AI 배치·검사 → 줄과 칸 → 결과 콜백 본문 dict
  previousLines 가 있으면 다시 배치 (명세 8장): 재료는 이전 줄에서 만들고(relayout.py),
  이전 초안에 없던 요약 항목은 AI 에 보내지 않고 unplaced 로, AI 가 넣지 못한 이전 줄은 unplacedLines 로 돌려준다

  재료가 0개        AI 를 부르지 않고 AI 가 채우는 필드는 모두 "해당 내용 없음"
  AI 채움 필드 0개  AI 를 부르지 않고 모든 재료를 unplaced(이전 줄은 unplacedLines)로
양식 필드 (명세 v0.7): AI 에는 AI 가 채우는 필드만 칸 목록으로 넘기고, 돌아온 칸 번호를 양식 필드 위치로 되돌린다.
사용자가 채우는 필드·머리글·바닥글은 받은 그대로 돌려준다. v0.6 요청(template.items)은 결과도 v0.6 모양이다.
  재료가 상한 초과  AI 를 부르지 않고 FAILED / INPUT_TOO_LONG (AI 에 넘길 재료 수. 다시 배치는 이전 줄 수)
  AI 실패           FAILED / LLM_TIMEOUT · LLM_FAILED · CHECK_FAILED

요청 검사(형식 오류 → 400)는 이 함수를 부르기 전에 한다 (request.validate_request). 여기서는 다시 하지 않는다.
로그에는 요청 번호, 회의록 번호, 개수, 시도 횟수, 상태, 오류 코드, 토큰, 걸린 시간만 남긴다. 요약·칸 이름·초안 글은 남기지 않는다.
"""
import logging
import time

from .lines import build_sections, unplaced_lines
from .materials import build_materials
from .models import Placement
from .placement import RELAYOUT_KIND, Prompt, load_prompt, place
from .relayout import build_relayout
from .request import template_fields

log = logging.getLogger("minutes.draft")

MAX_MATERIALS = 300
INPUT_TOO_LONG = "INPUT_TOO_LONG"
INTERNAL_ERROR = "INTERNAL_ERROR"               # 작업자가 쓴다: 예상하지 못한 오류로 다시 시도해도 끝내 못 만듦
SUCCEEDED, FAILED = "SUCCEEDED", "FAILED"
MESSAGES = {                                    # 실패 설명. 요약·초안 내용은 넣지 않는다
    INPUT_TOO_LONG: "요약 항목이나 이전 줄이 너무 많음",
    INTERNAL_ERROR: "초안을 만드는 중 알 수 없는 오류",
    "LLM_TIMEOUT": "AI 응답 시간 초과",
    "LLM_FAILED": "AI 호출 실패",
    "CHECK_FAILED": "AI 결과가 검사를 통과하지 못함",
}


def make_draft(body: dict, llm, timeout_s: float = 60.0, max_retries: int = 2, prompt: Prompt | None = None,
               call_retries: int = 1, relayout_prompt: Prompt | None = None) -> dict:
    """결과 콜백 본문(명세 3.1·3.2)을 돌려준다. 다시 배치에는 relayout_prompt(없으면 relayout 의 가장 큰 번호)를 쓴다."""
    t0 = time.monotonic()
    request_id, template = body["requestId"], body["template"]
    fields, v07 = template_fields(template)
    ai_fields = [i for i, f in enumerate(fields) if f.ai_fill]        # AI 가 보는 칸 번호 → 양식 필드 위치
    items = [fields[i].title for i in ai_fields]
    extra = {"header": template.get("header"), "footer": template.get("footer")} if v07 else None
    previous = body.get("previousLines") or []
    if previous:
        materials, unreferenced = build_relayout(body["agendas"], body["summary"], previous)
    else:
        materials, unreferenced = build_materials(body["agendas"], body["summary"]), []
    tag = (f"request_id={request_id} minutes={body.get('minutesId')} fields={len(fields)} ai_fields={len(items)}"
           f" materials={len(materials)}"
           f" previous={len(previous)}")
    not_sent = [m.source() for m in unreferenced]      # 이전 초안에 없던 요약 항목 (다시 배치)

    if len(materials) > MAX_MATERIALS:
        log.warning("초안 실패 %s error=%s", tag, INPUT_TOO_LONG)
        return failed(request_id, INPUT_TOO_LONG, f"{MESSAGES[INPUT_TOO_LONG]} ({len(materials)}개, 상한 {MAX_MATERIALS}개)")

    if not materials or not items:
        # 재료가 없거나 AI 가 채울 필드가 없으면 AI 를 부르지 않는다. 재료는 모두 넣지 못한 것으로 돌려준다
        nothing = Placement(sections={}, texts={}, unplaced=tuple(m.ref for m in materials))
        sections, unplaced = build_sections(fields, materials, nothing, v07)
        log.info("초안 완료 %s attempts=0 ms=%d", tag, _ms(t0))
        return succeeded(request_id, sections, not_sent + unplaced, unplaced_lines(materials, nothing, previous), extra)

    if previous:
        prompt = relayout_prompt or load_prompt(kind=RELAYOUT_KIND)
    else:
        prompt = prompt or load_prompt()
    outcome = place(items, materials, llm, timeout_s, max_retries, prompt, call_retries)
    tag += (f" prompt={prompt.version} attempts={outcome.attempts}"
            f" in_tokens={outcome.input_tokens} out_tokens={outcome.output_tokens}")
    if outcome.placement is None:
        log.warning("초안 실패 %s error=%s ms=%d", tag, outcome.error_code, _ms(t0))
        return failed(request_id, outcome.error_code, MESSAGES[outcome.error_code])

    placement = outcome.placement
    placement = Placement(sections={ref: ai_fields[i] for ref, i in placement.sections.items()},
                          texts=placement.texts, unplaced=placement.unplaced)
    sections, unplaced = build_sections(fields, materials, placement, v07)
    unplaced = not_sent + unplaced
    lost_lines = unplaced_lines(materials, placement, previous)
    lines = sum(len(s["evidence"]) for s in sections)
    log.info("초안 완료 %s lines=%d unplaced=%d unplaced_lines=%d ms=%d",
             tag, lines, len(unplaced), len(lost_lines), _ms(t0))
    return succeeded(request_id, sections, unplaced, lost_lines, extra)


def succeeded(request_id, sections, unplaced, unplaced_lines_=(), extra=None) -> dict:
    """extra 는 v0.7 요청의 머리글·바닥글. v0.6 요청이면 None 이고 결과에 그 칸이 없다."""
    head = {"requestId": request_id, "status": SUCCEEDED, **(extra or {})}
    return {**head, "sections": sections, "unplaced": unplaced, "unplacedLines": list(unplaced_lines_)}


def failed(request_id, code, message) -> dict:
    return {"requestId": request_id, "status": FAILED, "error": {"code": code, "message": message}}


def _ms(since):
    return round((time.monotonic() - since) * 1000)
