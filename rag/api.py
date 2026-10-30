"""RAG 내부 API (백엔드 → AI 서버). 원본 명세: 노션 "RAG 인터페이스 명세".

  POST   {prefix}/documents                 문서 넣기 → 202 {docId, docVersion, status: "accepted" | "ignored"}
  GET    {prefix}/documents/{docId}         상태 조회 → 200 / 404
  DELETE {prefix}/documents/{docId}         문서 지우기 → 204 (없어도)
  DELETE {prefix}/workspaces/{workspaceId}  워크스페이스 지우기 → 204
  POST   {prefix}/answer                    답변 → 200 {status, answer, citations, errorCode}

업로드 문서(sourceType=upload)는 허용 저장소(RAG_UPLOAD_ALLOWED_HOSTS)가 설정됐을 때만 받는다. 없으면 400.
파일 받기·형식 확인까지 하고, 형식별 읽기(SCRUM-107~109)가 생기기 전까지는 unsupported_format 으로 끝난다.
회의록은 확정 회의록 형식(formatVersion "1.0")과 임시 형식(schema_version "0.1")을 받는다. 둘 다 아니면 400.
저장된 것보다 낮은 회의록 버전은 처리하지 않고 status "ignored" 로 답한다 (콜백 없음). 회의록 초안 명세 4.3.
명세에 없어서 AI 쪽이 정한 것: 입력 오류 400 INVALID_INPUT, 다른 워크스페이스로 온 같은 docId 409 CONFLICT,
DB 를 쓸 수 없음 503 NOT_READY. 오류 형식은 server/main.py 와 같다.
답변은 answered·no_result·failed 모두 200 이고 본문의 status 로 구분한다. ID 는 명세 문구와 달리 모두 정수다.

본문은 dict 로 받아 직접 검사한다 (server/ 쪽과 같은 방식, 앱의 검사 오류 처리기에 기대지 않으려고).
로그에는 docId, 버전, 결과, 요청 추적 ID 만 남긴다. 회의록 내용은 남기지 않는다.
"""
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Request
from fastapi.responses import JSONResponse, Response

from .callback import CallbackNotAllowed
from .download import UPLOAD_OFF, FileUrlNotAllowed, UploadSettings, check_file_url
from .errors import IngestError
from .jobs import IngestRequest, submit
from .loaders.minutes import check_format
from .store import DocumentMeta, WorkspaceMismatch, delete_document, delete_workspace, get_status

log = logging.getLogger("rag.api")

PREFIX = "/internal/rag"
REQUEST_ID_HEADER = "X-Request-Id"
MAX_REQUEST_ID = 200
MAX_DOC_VERSION = 200
MAX_TITLE = 500
MAX_QUERY = 2000
MAX_DOC_IDS = 1000
DEFAULT_TOP_K = 5
MAX_TOP_K = 20                       # search.MAX_TOP_K 와 같다
SOURCE_TYPES = ("minutes", "upload")  # upload 는 허용 저장소(RAG_UPLOAD_ALLOWED_HOSTS)가 있을 때만 받는다
API_STATUS = {"queued": "accepted"}  # DB 상태 → 명세의 상태 값


class InvalidInput(ValueError):
    pass


# ---- /docs 화면용 설명 (검사는 parse_ingest 가 직접 한다) ----

INGEST_EXAMPLE = {
    "docId": 200, "docVersion": "2", "workspaceId": 1, "sourceType": "minutes", "title": "10월 예산 회의",
    "minutes": {"formatVersion": "1.0", "meetingId": 31, "meetingStartAt": "2026-10-20T14:00:00",
                "confirmedAt": "2026-10-21T09:30:00", "sections": [
                    {"title": "결정 사항", "content": "- 예산: 1분기 예산은 3천만 원으로 정했다."},
                    {"title": "할 일", "content": "- 예산: 광고 집행 계획 공유 (이영희, 금요일까지)"}]},
    "callbackUrl": "https://backend.internal/ai/ingest-result",
}
INGEST_SCHEMA = {
    "type": "object",
    "required": ["docId", "docVersion", "workspaceId", "sourceType", "title", "minutes", "callbackUrl"],
    "properties": {
        "docId": {"type": "integer", "minimum": 1, "description": "백엔드가 발급한 문서 ID"},
        "docVersion": {"type": "string", "maxLength": MAX_DOC_VERSION,
                       "description": "회의록: 확정 버전 번호 / 업로드 문서: 파일 해시"},
        "workspaceId": {"type": "integer", "minimum": 1, "description": "소속 워크스페이스"},
        "sourceType": {"type": "string", "enum": list(SOURCE_TYPES),
                       "description": "minutes 또는 upload. upload 는 허용 저장소 설정이 있을 때만 받는다 (없으면 400)"},
        "title": {"type": "string", "maxLength": MAX_TITLE, "description": "회의 제목 (업로드 문서는 파일 이름)"},
        "minutes": {"type": "object", "description": "확정 회의록 (formatVersion \"1.0\": meetingId, meetingStartAt, "
                    "confirmedAt, sections[{title, content}]). 칸의 줄 하나가 검색 조각 하나. "
                    "임시 형식 summary.json (schema_version \"0.1\")도 전환 기간에는 받는다"},
        "fileUrl": {"type": "string", "description": "업로드 문서의 S3 서명 주소. 허용한 저장소의 주소만. pdf·docx·xlsx (형식별 읽기는 준비 중)"},
        "callbackUrl": {"type": "string", "description": "처리가 끝나면 AI 서버가 POST 할 백엔드 주소. 허용한 호스트만"},
    },
}
STATUS_EXAMPLE = {"docId": 200, "docVersion": "2", "status": "ready", "errorCode": None, "chunkCount": 4}
ANSWER_EXAMPLE = {"workspaceId": 1, "docIds": [200, 123], "query": "1분기 예산은 얼마로 하기로 했어?", "topK": 5}
ANSWER_SCHEMA = {
    "type": "object",
    "required": ["workspaceId", "docIds", "query"],
    "properties": {
        "workspaceId": {"type": "integer", "minimum": 1, "description": "소속 워크스페이스"},
        "docIds": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": MAX_DOC_IDS,
                   "description": "이 사용자가 볼 수 있는 문서 목록. 비면 no_result"},
        "query": {"type": "string", "maxLength": MAX_QUERY, "description": "사용자 질문"},
        "topK": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K, "default": DEFAULT_TOP_K,
                 "description": "근거로 찾을 조각 수"},
    },
}
ANSWER_RESULT_EXAMPLE = {
    "status": "answered",
    "answer": "1분기 예산은 3천만 원으로 하자는 제안이 회의에서 나왔습니다 [1].",
    "citations": [{"ref": 1, "docId": 200, "sourceType": "minutes", "title": "10월 예산 회의",
                   "text": "1분기 예산은 3천만 원으로 하자는 제안이 나옴",
                   "location": {"type": "time", "startMs": 754000, "endMs": 791000, "label": "12:34~13:11"}}],
    "errorCode": None,
}


def _error_doc(description, error, detail):
    return {"description": description,
            "content": {"application/json": {"example": {"error": error, "detail": detail}}}}


ERR_400 = _error_doc("입력 오류", "INVALID_INPUT", "docId 는 양의 정수여야 함")
ERR_503 = _error_doc("DB 를 쓸 수 없음", "NOT_READY", "DB 를 쓸 수 없음")
DOC_ID = Path(description="문서 번호 (양의 정수)", examples=["200"])
WORKSPACE_ID = Path(description="워크스페이스 번호 (양의 정수)", examples=["1"])


def invalid(detail):
    return JSONResponse(status_code=400, content={"error": "INVALID_INPUT", "detail": detail})


def conflict(detail):
    return JSONResponse(status_code=409, content={"error": "CONFLICT", "detail": detail})


def not_ready(detail):
    return JSONResponse(status_code=503, content={"error": "NOT_READY", "detail": detail})


def verify_internal_request(request: Request):
    """서버 간 인증. CN팀 방식이 정해지면 여기서 검사한다. 지금은 통과시킨다."""
    return None


def parse_ingest(body, request_id=None, upload: UploadSettings | None = None) -> IngestRequest:
    if not isinstance(body, dict):
        raise InvalidInput("본문이 JSON 객체가 아님")
    doc_id = _positive_int(body, "docId")
    workspace_id = _positive_int(body, "workspaceId")
    version = _text(body, "docVersion", MAX_DOC_VERSION)
    title = _text(body, "title", MAX_TITLE)
    source = body.get("sourceType")
    if source not in SOURCE_TYPES:
        raise InvalidInput("sourceType 은 minutes 나 upload 여야 함")
    callback_url = body.get("callbackUrl")
    if not isinstance(callback_url, str) or not callback_url:
        raise InvalidInput("callbackUrl 이 없음")
    doc = DocumentMeta(doc_id=doc_id, doc_version=version, workspace_id=workspace_id, source_type=source, title=title)
    if source == "upload":
        file_url = body.get("fileUrl")
        try:
            check_file_url(file_url, upload or UPLOAD_OFF)
        except FileUrlNotAllowed as e:
            raise InvalidInput(str(e)) from None
        return IngestRequest(doc, callback_url, file_url=file_url, request_id=request_id)
    minutes = body.get("minutes")
    if not isinstance(minutes, dict):
        raise InvalidInput("minutes 는 JSON 객체여야 함")
    try:
        check_format(minutes)
    except IngestError as e:
        raise InvalidInput(f"minutes 의 {e.detail}") from None
    return IngestRequest(doc, callback_url, minutes=minutes, request_id=request_id)


def parse_answer(body):
    """답변 요청 → (workspaceId, docIds, query, topK). history 등 다른 필드는 무시한다."""
    if not isinstance(body, dict):
        raise InvalidInput("본문이 JSON 객체가 아님")
    workspace_id = _positive_int(body, "workspaceId")
    doc_ids = body.get("docIds")
    if not isinstance(doc_ids, list):
        raise InvalidInput("docIds 는 배열이어야 함")
    if len(doc_ids) > MAX_DOC_IDS:
        raise InvalidInput(f"docIds 는 {MAX_DOC_IDS}개 이하여야 함")
    if any(not _is_positive_int(d) for d in doc_ids):
        raise InvalidInput("docIds 의 값은 양의 정수여야 함")
    query = _text(body, "query", MAX_QUERY)
    top_k = body.get("topK", DEFAULT_TOP_K)
    if not _is_positive_int(top_k) or top_k > MAX_TOP_K:
        raise InvalidInput(f"topK 는 1~{MAX_TOP_K} 정수여야 함")
    return workspace_id, doc_ids, query, top_k


def _is_positive_int(v):
    return not isinstance(v, bool) and isinstance(v, int) and v > 0     # bool 은 int 의 하위 형이라 따로 막는다


def _positive_int(body, key):
    v = body.get(key)
    if not _is_positive_int(v):
        raise InvalidInput(f"{key} 는 양의 정수여야 함")
    return v


def _text(body, key, max_len):
    v = body.get(key)
    if not isinstance(v, str) or not v.strip():
        raise InvalidInput(f"{key} 는 비어 있지 않은 문자열이어야 함")
    if len(v) > max_len:
        raise InvalidInput(f"{key} 는 {max_len}자 이하여야 함")
    return v


def _path_id(raw, name):
    if not raw.isdigit() or int(raw) <= 0:
        raise InvalidInput(f"{name} 는 양의 정수여야 함")
    return int(raw)


def make_router(pool, allowed_callback_hosts=None, request_id_header=REQUEST_ID_HEADER, prefix=PREFIX, answerer=None,
                upload: UploadSettings | None = None):
    """pool: psycopg_pool.ConnectionPool (autocommit 연결, vector 형 등록).
    answerer: answer.Answerer. 없으면 답변 주소는 503 으로 답한다.
    upload: 업로드 문서 설정. 없거나 허용 저장소가 없으면 sourceType=upload 는 400."""
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    db_down = (OperationalError, PoolTimeout)
    router = APIRouter(prefix=prefix, tags=["RAG"], dependencies=[Depends(verify_internal_request)])

    def request_id_of(request):
        rid = request.headers.get(request_id_header)
        return rid[:MAX_REQUEST_ID] if rid else None

    @router.post(
        "/documents", summary="문서 넣기",
        description="회의록을 검색 대상으로 접수한다. 바로 accepted 로 답하고, 처리가 끝나면 callbackUrl 로 결과를 알린다. "
                    "같은 docId·docVersion 을 다시 보내도 결과는 하나다.",
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
            "schema": INGEST_SCHEMA, "example": INGEST_EXAMPLE}}}},
        responses={202: {"description": "접수됨 (accepted). 저장된 것보다 낮은 회의록 버전이면 처리하지 않음 (ignored, 콜백 없음)",
                         "content": {"application/json": {
                             "example": {"docId": 200, "docVersion": "2", "status": "accepted"}}}},
                   400: ERR_400,
                   409: _error_doc("같은 docId 가 다른 워크스페이스로 옴", "CONFLICT", "docId 200 의 workspaceId 가 기존과 다름"),
                   503: ERR_503},
    )
    def post_document(request: Request, body: Any = Body(...)):
        rid = request_id_of(request)
        try:
            req = parse_ingest(body, rid, upload)
            with pool.connection() as conn:
                result = submit(conn, req, allowed_callback_hosts)
        except (InvalidInput, CallbackNotAllowed) as e:
            log.info("문서 넣기 거부 request_id=%s reason=%s", rid, e)
            return invalid(str(e))
        except WorkspaceMismatch as e:
            log.warning("문서 넣기 충돌 request_id=%s reason=%s", rid, e)
            return conflict(str(e))
        except db_down as e:
            log.error("문서 넣기 DB 오류 request_id=%s error=%s", rid, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        log.info("문서 넣기 doc=%s version=%s status=%s enqueue=%s ignored=%s request_id=%s",
                 req.doc.doc_id, req.doc.doc_version, result.status, result.enqueue, result.ignored, rid)
        return JSONResponse(status_code=202, content={"docId": req.doc.doc_id, "docVersion": req.doc.doc_version,
                                                      "status": "ignored" if result.ignored else "accepted"})

    @router.get(
        "/documents/{doc_id}", summary="상태 조회",
        description="status: accepted(접수됨) / processing(처리 중) / ready(검색 가능) / failed(실패, errorCode 참고)",
        responses={200: {"description": "문서 상태", "content": {"application/json": {"example": STATUS_EXAMPLE}}},
                   400: ERR_400,
                   404: _error_doc("넣은 적 없는 문서", "NOT_FOUND", "넣은 적 없는 문서"),
                   503: ERR_503},
    )
    def get_document(doc_id: str = DOC_ID):
        try:
            did = _path_id(doc_id, "docId")
            with pool.connection() as conn:
                status = get_status(conn, did)
        except InvalidInput as e:
            return invalid(str(e))
        except db_down as e:
            log.error("상태 조회 DB 오류 doc=%s error=%s", doc_id, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        if status is None:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "detail": "넣은 적 없는 문서"})
        status["status"] = API_STATUS.get(status["status"], status["status"])
        return status

    @router.delete("/documents/{doc_id}", summary="문서 지우기", status_code=204,
                   description="문서와 저장한 조각, 대기 중인 작업을 지운다. 없는 문서여도 204.",
                   responses={400: ERR_400, 503: ERR_503})
    def remove_document(request: Request, doc_id: str = DOC_ID):
        try:
            did = _path_id(doc_id, "docId")
            with pool.connection() as conn:
                delete_document(conn, did)
        except InvalidInput as e:
            return invalid(str(e))
        except db_down as e:
            log.error("문서 지우기 DB 오류 doc=%s error=%s", doc_id, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        log.info("문서 지우기 doc=%s request_id=%s", did, request_id_of(request))
        return Response(status_code=204)

    @router.delete("/workspaces/{workspace_id}", summary="워크스페이스 지우기", status_code=204,
                   description="그 워크스페이스의 문서·조각·작업을 모두 지운다.",
                   responses={400: ERR_400, 503: ERR_503})
    def remove_workspace(request: Request, workspace_id: str = WORKSPACE_ID):
        try:
            wid = _path_id(workspace_id, "workspaceId")
            with pool.connection() as conn:
                removed = delete_workspace(conn, wid)
        except InvalidInput as e:
            return invalid(str(e))
        except db_down as e:
            log.error("워크스페이스 지우기 DB 오류 workspace=%s error=%s", workspace_id, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        log.info("워크스페이스 지우기 workspace=%s 문서=%d request_id=%s", wid, removed, request_id_of(request))
        return Response(status_code=204)

    @router.post(
        "/answer", summary="답변",
        description="볼 수 있는 문서(docIds) 안에서 질문과 가까운 조각을 찾아, LLM 이 그 조각만 근거로 답한다. "
                    "답의 문장마다 근거 번호 [n] 이 붙고, citations 에 그 번호의 원문과 위치가 담긴다. "
                    "status: answered(답함) / no_result(근거 없음, LLM 을 부르지 않음) / failed(errorCode 참고). "
                    "세 경우 모두 200 이다. 지금 LLM 은 시험용 가짜라 답 앞에 [시험용 답변] 이 붙는다.",
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
            "schema": ANSWER_SCHEMA, "example": ANSWER_EXAMPLE}}}},
        responses={200: {"description": "답변 결과 (status 로 구분)", "content": {"application/json": {
                       "example": ANSWER_RESULT_EXAMPLE}}},
                   400: _error_doc("입력 오류", "INVALID_INPUT", "docIds 의 값은 양의 정수여야 함"),
                   503: _error_doc("DB·임베딩 모델·LLM 설정이 준비되지 않음", "NOT_READY", "DB 를 쓸 수 없음")},
    )
    def post_answer(request: Request, body: Any = Body(...)):
        rid = request_id_of(request)
        try:
            workspace_id, doc_ids, query, top_k = parse_answer(body)
        except InvalidInput as e:
            log.info("답변 거부 request_id=%s reason=%s", rid, e)
            return invalid(str(e))
        reason = "답변 기능이 켜지지 않음" if answerer is None else answerer.not_ready_reason()
        if reason:
            log.error("답변 준비 안 됨 request_id=%s reason=%s", rid, reason)
            return not_ready(reason)
        try:
            return answerer.run(pool.connection, workspace_id, doc_ids, query, top_k, request_id=rid)
        except db_down as e:
            log.error("답변 DB 오류 request_id=%s error=%s", rid, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")

    return router
