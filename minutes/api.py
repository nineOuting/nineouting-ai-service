"""회의록 초안 내부 API. 명세 2장 (docs/api/minutes-draft-contract.md)

  POST /internal/minutes/drafts   초안 생성 요청 접수. 202 로 바로 답하고 결과는 callbackUrl 로 알린다

  202  {"requestId", "status": "ACCEPTED"}    접수함
  202  {"requestId", "status": "DUPLICATE"}   같은 requestId 가 처리 중이거나 성공 결과를 이미 보냄 (실패로 끝났으면 새로 받음)
  400  INVALID_INPUT   요청 형식 오류, 허용하지 않은 callbackUrl (오류 글에는 칸 위치만, 값은 넣지 않는다)
  503  NOT_READY       DB 를 쓸 수 없거나, 대기열 표가 없거나(마이그레이션 안 함), AI 설정이 틀림

로그에는 요청 번호, 회의록 번호, 결과, 추적 번호만 남긴다. 요약·초안 내용은 남기지 않는다.
"""
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from rag.callback import CallbackNotAllowed, check_callback_url

from .jobs import submit
from .request import InvalidInput, validate_request

log = logging.getLogger("minutes.api")

PREFIX = "/internal/minutes"
REQUEST_ID_HEADER = "X-Request-Id"
MAX_TRACE_ID = 200

EXAMPLE = {
    "requestId": "6f1c2a9e-1b7d-4c55-9d1a-3a8e0f4b2c10", "minutesId": 41, "meetingId": 31,
    "template": {"items": ["안건별 논의", "결정 사항", "미결 안건", "할 일"]},
    "agendas": [{"agendaId": 101, "title": "배포 일정", "decisionPoint": "배포 날짜 확정"}],
    "summary": {"requestRev": 3, "final": True, "agendas": [{
        "agendaId": 101, "status": "DECIDED",
        "statusReason": {"text": "1차 배포를 10월 30일(목)로 정하고 참석자가 동의함", "evidenceSeqs": [40, 42]},
        "summary": [{"id": "k3f9a1-1", "text": "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴",
                     "evidenceSeqs": [38, 39], "edited": False, "needsCheck": False}],
        "opinions": [], "todos": []}]},
    "previousLines": [],
    "callbackUrl": "http://minutes-service:8084/internal/minutes/41/draft-result",
}


def _error_doc(description, error, detail):
    return {"description": description,
            "content": {"application/json": {"example": {"error": error, "detail": detail}}}}


def invalid(detail):
    return JSONResponse(status_code=400, content={"error": "INVALID_INPUT", "detail": detail})


def not_ready(detail):
    return JSONResponse(status_code=503, content={"error": "NOT_READY", "detail": detail})


def verify_internal_request(request: Request):
    """서버 간 인증. CN팀 방식이 정해지면 여기서 검사한다. 지금은 통과시킨다 (RAG 와 같음)."""
    return None


def make_router(pool, allowed_callback_hosts=None, request_id_header=REQUEST_ID_HEADER, prefix=PREFIX,
                config_error=None):
    """pool: psycopg_pool.ConnectionPool (autocommit 연결).
    config_error: AI 설정 오류 글. 있으면 요청을 받지 않고 503 으로 답한다 (받아도 처리할 수 없으므로)."""
    from psycopg import OperationalError
    from psycopg.errors import UndefinedTable
    from psycopg_pool import PoolTimeout

    db_down = (OperationalError, PoolTimeout)
    router = APIRouter(prefix=prefix, tags=["회의록 초안"], dependencies=[Depends(verify_internal_request)])

    @router.post(
        "/drafts", summary="초안 생성 요청",
        description="점검을 마친 회의 끝 요약을 양식의 칸에 나눠 담아 회의록 초안을 만든다. 바로 202 로 답하고, "
                    "다 만들면 callbackUrl 로 결과(SUCCEEDED / FAILED)를 보낸다. summary 는 안건 보드의 todaySummary 칸 그대로.",
        status_code=202,
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"example": EXAMPLE}}}},
        responses={202: {"description": "접수함 (ACCEPTED) 또는 이미 받음 (DUPLICATE)", "content": {"application/json": {
                       "example": {"requestId": EXAMPLE["requestId"], "status": "ACCEPTED"}}}},
                   400: _error_doc("입력 오류", "INVALID_INPUT", "template.items[1]의 칸 이름이 앞의 칸과 겹침"),
                   503: _error_doc("DB 나 AI 설정이 준비되지 않음", "NOT_READY", "DB 를 쓸 수 없음")},
    )
    def post_draft(request: Request, body: Any = Body(...)):
        trace = request.headers.get(request_id_header)
        trace = trace[:MAX_TRACE_ID] if trace else None
        if config_error:
            return not_ready("AI 설정이 준비되지 않음")
        try:
            validate_request(body)
            check_callback_url(body["callbackUrl"], allowed_callback_hosts)
        except (InvalidInput, CallbackNotAllowed) as e:
            log.info("초안 요청 거부 trace=%s reason=%s", trace, e)
            return invalid(str(e))
        try:
            with pool.connection() as conn:
                status = submit(conn, body, trace)
        except db_down as e:
            log.error("초안 요청 DB 오류 request_id=%s trace=%s error=%s", body["requestId"], trace, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        except UndefinedTable:                       # DB 는 있는데 마이그레이션을 하지 않음
            log.error("초안 요청 대기열 표 없음 request_id=%s trace=%s, python -m minutes.migrate 필요",
                      body["requestId"], trace)
            return not_ready("대기열 표 없음")
        log.info("초안 요청 request_id=%s minutes=%s status=%s trace=%s", body["requestId"], body["minutesId"], status, trace)
        return JSONResponse(status_code=202, content={"requestId": body["requestId"], "status": status})

    return router
