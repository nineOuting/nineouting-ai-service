"""회의록 초안 만들기 전체 흐름. 명세 2·3·6·7장

  요청 dict → 재료 → (재료가 있으면) AI 배치·검사 → 줄과 칸 → 결과 콜백 본문 dict

  재료가 0개        AI 를 부르지 않고 모든 칸 "해당 내용 없음"
  재료가 상한 초과  AI 를 부르지 않고 FAILED / INPUT_TOO_LONG
  AI 실패           FAILED / LLM_TIMEOUT · LLM_FAILED · CHECK_FAILED

요청 검사(형식 오류 → 400)는 이 함수를 부르기 전에 한다 (request.validate_request). 여기서는 다시 하지 않는다.
로그에는 요청 번호, 회의록 번호, 개수, 시도 횟수, 상태, 오류 코드, 토큰, 걸린 시간만 남긴다. 요약·칸 이름·초안 글은 남기지 않는다.
"""
import logging
import time

from .lines import build_sections
from .materials import build_materials
from .models import Placement
from .placement import Prompt, load_prompt, place

log = logging.getLogger("minutes.draft")

MAX_MATERIALS = 300
INPUT_TOO_LONG = "INPUT_TOO_LONG"
INTERNAL_ERROR = "INTERNAL_ERROR"               # 작업자가 쓴다: 예상하지 못한 오류로 다시 시도해도 끝내 못 만듦
SUCCEEDED, FAILED = "SUCCEEDED", "FAILED"
MESSAGES = {                                    # 실패 설명. 요약·초안 내용은 넣지 않는다
    INPUT_TOO_LONG: "요약 항목이 너무 많음",
    INTERNAL_ERROR: "초안을 만드는 중 알 수 없는 오류",
    "LLM_TIMEOUT": "AI 응답 시간 초과",
    "LLM_FAILED": "AI 호출 실패",
    "CHECK_FAILED": "AI 결과가 검사를 통과하지 못함",
}


def make_draft(body: dict, llm, timeout_s: float = 60.0, max_retries: int = 2, prompt: Prompt | None = None,
               call_retries: int = 1) -> dict:
    """결과 콜백 본문(명세 3.1·3.2)을 돌려준다."""
    t0 = time.monotonic()
    request_id, items = body["requestId"], body["template"]["items"]
    materials = build_materials(body["agendas"], body["summary"])
    tag = f"request_id={request_id} minutes={body.get('minutesId')} items={len(items)} materials={len(materials)}"

    if len(materials) > MAX_MATERIALS:
        log.warning("초안 실패 %s error=%s", tag, INPUT_TOO_LONG)
        return failed(request_id, INPUT_TOO_LONG, f"{MESSAGES[INPUT_TOO_LONG]} ({len(materials)}개, 상한 {MAX_MATERIALS}개)")

    if not materials:
        sections, unplaced = build_sections(items, [], Placement(sections={}, texts={}, unplaced=()))
        log.info("초안 완료 %s attempts=0 ms=%d", tag, _ms(t0))
        return succeeded(request_id, sections, unplaced)

    prompt = prompt or load_prompt()
    outcome = place(items, materials, llm, timeout_s, max_retries, prompt, call_retries)
    tag += (f" prompt={prompt.version} attempts={outcome.attempts}"
            f" in_tokens={outcome.input_tokens} out_tokens={outcome.output_tokens}")
    if outcome.placement is None:
        log.warning("초안 실패 %s error=%s ms=%d", tag, outcome.error_code, _ms(t0))
        return failed(request_id, outcome.error_code, MESSAGES[outcome.error_code])

    sections, unplaced = build_sections(items, materials, outcome.placement)
    lines = sum(len(s["evidence"]) for s in sections)
    log.info("초안 완료 %s lines=%d unplaced=%d ms=%d", tag, lines, len(unplaced), _ms(t0))
    return succeeded(request_id, sections, unplaced)


def succeeded(request_id, sections, unplaced) -> dict:
    return {"requestId": request_id, "status": SUCCEEDED, "sections": sections, "unplaced": unplaced,
            "unplacedLines": []}


def failed(request_id, code, message) -> dict:
    return {"requestId": request_id, "status": FAILED, "error": {"code": code, "message": message}}


def _ms(since):
    return round((time.monotonic() - since) * 1000)
