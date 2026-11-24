"""회의록 양식 가져오기 내부 API (SCRUM-289, docs/api/template-import-contract.md)

  POST /internal/minutes/template-imports   양식 파일 하나를 읽어 양식 항목을 돌려준다. 동기 (호출 1번에 보통 4~25초)

요청  {"requestId": "...(선택, 로그용)", "fileUrl": "https://<허용한 저장소>/...?서명"}
200   {"requestId", "status": "SUCCEEDED", "fields": [...], "header", "footer", "warnings": [...]}
200   {"requestId", "status": "FAILED", "errorCode": "unsupported_format" | "encrypted" | "too_large" | "no_text" | "NO_FIELDS" |
                     "CHECK_FAILED" | "LLM_TIMEOUT" | "LLM_FAILED" | "url_expired" | "download_failed" | "internal_error"}
400   INVALID_INPUT   요청 형식 오류, 허용하지 않은 fileUrl (오류 글에 주소를 넣지 않는다)
503   NOT_READY       파일 받기나 AI 설정이 준비되지 않음
503   BUSY            동시에 처리하는 양식이 가득 참. 잠시 뒤 다시 보낸다

파일은 fileUrl 에서 메모리로만 받는다 (디스크에 쓰지 않는다). 허용 저장소는 RAG 업로드와 같은 RAG_UPLOAD_ALLOWED_HOSTS 를 쓴다.
로그에는 요청 번호, 결과, 오류 코드, 호출 횟수, 토큰 수, 걸린 시간만 남긴다. fileUrl(접근 서명이 들어 있음)과 양식 글은 남기지 않는다.
"""
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from rag.download import FileUrlNotAllowed, UploadSettings, check_file_url, fetch
from rag.errors import INTERNAL_ERROR, IngestError

from .api import MAX_TRACE_ID, PREFIX, REQUEST_ID_HEADER, invalid, not_ready, verify_internal_request
from .template_import import import_template

log = logging.getLogger("minutes.import")

PATH = "/template-imports"
MAX_REQUEST_ID = 200
DEFAULT_MAX_CONCURRENT = 4

EXAMPLE = {"requestId": "6f1c2a9e-1b7d-4c55-9d1a-3a8e0f4b2c11",
           "fileUrl": "https://nineouting-dev-files.s3.ap-northeast-2.amazonaws.com/templates/form.pdf?X-Amz-Signature=..."}
EXAMPLE_OK = {"requestId": EXAMPLE["requestId"], "status": "SUCCEEDED",
              "fields": [{"title": "논의 내용", "type": "free", "description": "※ 안건별로 적는다", "aiFill": True},
                         {"title": "할 일", "type": "table", "columns": 3, "aiFill": True}],
              "header": "나인아웃팅 회의록", "footer": "- 2 -",
              "warnings": [{"code": "CHECKLIST_NO_BOX", "title": "확인 사항"}]}
EXAMPLE_FAILED = {"requestId": EXAMPLE["requestId"], "status": "FAILED", "errorCode": "no_text"}


def _validate(body):
    """요청 형식을 검사해 (requestId 또는 None, fileUrl) 을 돌려준다. 틀리면 ValueError (글에 값을 넣지 않는다)."""
    if not isinstance(body, dict):
        raise ValueError("요청 본문이 JSON 객체가 아님")
    request_id = body.get("requestId")
    if request_id is not None and (not isinstance(request_id, str) or not 0 < len(request_id) <= MAX_REQUEST_ID):
        raise ValueError(f"requestId 는 1~{MAX_REQUEST_ID}자 글이어야 함")
    url = body.get("fileUrl")
    if not isinstance(url, str) or not url:
        raise ValueError("fileUrl 이 없음")
    return request_id, url


def _reply(request_id, **fields):
    out = {"requestId": request_id} if request_id else {}
    out.update(fields)
    return JSONResponse(status_code=200, content=out)


def make_import_router(llm, timeout_s: float, upload: UploadSettings, prompt=None, config_error=None,
                       max_concurrent: int = DEFAULT_MAX_CONCURRENT, request_id_header=REQUEST_ID_HEADER, prefix=PREFIX):
    """llm: shared.llm 의 LLM (None 이면 AI 설정 오류). upload: 파일을 받을 수 있는 저장소 설정."""
    router = APIRouter(prefix=prefix, tags=["회의록 양식 가져오기"], dependencies=[Depends(verify_internal_request)])
    slots = threading.BoundedSemaphore(max(1, max_concurrent))

    @router.post(
        PATH, summary="양식 파일에서 항목 읽기",
        description="서명 주소(fileUrl)로 양식 파일(PDF, 워드, 엑셀, txt·md·csv)을 받아 양식 항목(제목, 기록 방식, 표의 열 개수, 설명)과 "
                    "머리글·바닥글을 돌려준다. 동기로 답한다. 파일을 읽지 못했거나 항목을 못 찾은 것은 200 + FAILED 와 errorCode 로 알린다.",
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"example": EXAMPLE}}}},
        responses={200: {"description": "SUCCEEDED 또는 FAILED", "content": {"application/json": {"examples": {
                       "SUCCEEDED": {"value": EXAMPLE_OK}, "FAILED": {"value": EXAMPLE_FAILED}}}}},
                   400: {"description": "입력 오류", "content": {"application/json": {"example": {
                       "error": "INVALID_INPUT", "detail": "fileUrl 의 호스트가 허용한 저장소가 아님"}}}},
                   503: {"description": "준비 안 됨(NOT_READY) 또는 처리 자리 없음(BUSY)", "content": {"application/json": {
                       "example": {"error": "BUSY", "detail": "동시에 처리하는 양식이 가득 참"}}}}},
    )
    def post_template_import(request: Request, body: Any = Body(...)):
        trace = request.headers.get(request_id_header)
        trace = trace[:MAX_TRACE_ID] if trace else None
        if config_error or llm is None:
            return not_ready("AI 설정이 준비되지 않음")
        if not upload.enabled:
            return not_ready("파일을 받도록 설정되지 않음")
        try:
            request_id, url = _validate(body)
            check_file_url(url, upload)
        except (ValueError, FileUrlNotAllowed) as e:                # FileUrlNotAllowed 는 ValueError 이기도 하다
            log.info("양식 가져오기 요청 거부 trace=%s reason=%s", trace, e)
            return invalid(str(e))
        if not slots.acquire(blocking=False):
            log.warning("양식 가져오기 자리 없음 request_id=%s trace=%s", request_id, trace)
            return JSONResponse(status_code=503, content={"error": "BUSY", "detail": "동시에 처리하는 양식이 가득 참"})
        started = time.monotonic()
        attempts = tokens_in = tokens_out = fields = 0
        error_code, result = None, None
        try:
            try:
                data = fetch(url, upload)
                out = import_template(data, llm, timeout_s, prompt=prompt)
                attempts, tokens_in, tokens_out = out.attempts, out.input_tokens, out.output_tokens
                error_code, result = out.error_code, out.result
            except IngestError as e:                                 # 파일을 받지 못함 (url_expired, download_failed, too_large)
                error_code = e.code
            except Exception as e:                                   # noqa: BLE001  글이 깨진 파일 등 예상 못 한 오류도 서버를 흔들지 않는다
                log.error("양식 가져오기 내부 오류 request_id=%s trace=%s error=%s", request_id, trace, type(e).__name__)
                error_code = INTERNAL_ERROR
        finally:
            slots.release()
        if result is not None and error_code is None:
            fields = len(result.fields)
        log.info("양식 가져오기 request_id=%s trace=%s status=%s error=%s fields=%s attempts=%s tokens_in=%s tokens_out=%s sec=%.1f",
                 request_id, trace, "FAILED" if error_code else "SUCCEEDED", error_code, fields, attempts, tokens_in, tokens_out,
                 time.monotonic() - started)
        if error_code:
            return _reply(request_id, status="FAILED", errorCode=error_code)
        return _reply(request_id, status="SUCCEEDED", fields=result.fields, header=result.header, footer=result.footer,
                      warnings=result.warnings)

    return router
