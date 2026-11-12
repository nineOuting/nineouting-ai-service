"""RAG 내부 API (백엔드 → AI 서버). 원본 명세: 노션 "RAG 인터페이스 명세".

  POST   {prefix}/documents                          문서 넣기 → 202 {sourceType, docId, docVersion, status}
  GET    {prefix}/documents/{sourceType}/{docId}     상태 조회 → 200 / 404
  DELETE {prefix}/documents/{sourceType}/{docId}     문서 지우기 → 204 (없어도)
  DELETE {prefix}/workspaces/{workspaceId}  워크스페이스 지우기 → 204
  POST   {prefix}/answer                    답변 → 200 {status, answer, citations, errorCode}
  POST   {prefix}/chat                      회의 중 챗봇 → 200 {status, basis, answer, citations, errorCode}
                                            (명세 docs/api/rag-chat-contract.md)

업로드 문서(sourceType=upload)는 허용 저장소(RAG_UPLOAD_ALLOWED_HOSTS)가 설정됐을 때만 받는다. 없으면 400.
파일 받기·형식 확인까지 하고, 형식별 읽기(SCRUM-107~109)가 생기기 전까지는 unsupported_format 으로 끝난다.
회의록은 확정 회의록 형식(formatVersion "1.0")과 임시 형식(schema_version "0.1")을 받는다. 둘 다 아니면 400.
저장된 것보다 낮은 회의록 버전은 처리하지 않고 status "ignored" 로 답한다 (콜백 없음). 회의록 초안 명세 4.3.
명세에 없어서 AI 쪽이 정한 것: 입력 오류 400 INVALID_INPUT, 다른 워크스페이스로 온 같은 docId 409 CONFLICT,
DB 를 쓸 수 없음 503 NOT_READY. 오류 형식은 server/main.py 와 같다.
문서는 (sourceType, docId) 짝으로 구분한다 (회의 중 챗봇 명세 v0.2 9장). 회의록 200번과 첨부 자료 200번은 다른 문서다.
옛 모양(docIds, 번호만 있는 주소)은 받지 않는다. 번호만으로는 종류를 알 수 없다.
답변은 answered·no_result·failed 모두 200 이고 본문의 status 로 구분한다. ID 는 명세 문구와 달리 모두 정수다.

본문은 dict 로 받아 직접 검사한다 (server/ 쪽과 같은 방식, 앱의 검사 오류 처리기에 기대지 않으려고).
로그에는 docId, 버전, 결과, 요청 추적 ID 만 남긴다. 회의록 내용은 남기지 않는다.
"""
import logging
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Request
from fastapi.responses import JSONResponse, Response

from shared.callback import CallbackNotAllowed
from .chat import ROLES, ChatRequest, Turn
from .download import UPLOAD_OFF, FileUrlNotAllowed, UploadSettings, check_file_url
from .errors import IngestError
from .jobs import IngestRequest, submit
from .loaders.minutes import check_format, meeting_time
from .store import DocumentMeta, WorkspaceMismatch, delete_document, delete_workspace, get_status

log = logging.getLogger("rag.api")

PREFIX = "/internal/rag"
REQUEST_ID_HEADER = "X-Request-Id"
MAX_REQUEST_ID = 200
MAX_DOC_VERSION = 200
MAX_TITLE = 500
MAX_QUERY = 2000
MAX_DOCS = 1000                      # 답변·챗봇 요청의 docs (명세 v0.2 3.1)
DEFAULT_TOP_K = 5
MAX_TOP_K = 20                       # search.MAX_TOP_K 와 같다
SOURCE_TYPES = ("minutes", "upload")  # upload 는 허용 저장소(RAG_UPLOAD_ALLOWED_HOSTS)가 있을 때만 받는다
# 챗봇 상한 (명세 3.1. 정해 둔 값이며 SCRUM-224 에서 측정해 고친다). 아래 셋은 명세에 없어 AI 쪽이 정한 값이다
MAX_CHAT_QUERY = 1000
MAX_HISTORY = 20
MAX_LIVE_AGENDAS = 50
MAX_RECENT_LINES = 500
MAX_RECENT_CHARS = 30000
MAX_HISTORY_TEXT = 4000               # 대화 한 개의 글 (명세에 없음)
MAX_USER_NAME = 100                   # 명세에 없음
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
STATUS_EXAMPLE = {"sourceType": "minutes", "docId": 200, "docVersion": "2", "status": "ready", "errorCode": None, "chunkCount": 4}
ANSWER_EXAMPLE = {"workspaceId": 1, "docs": [{"sourceType": "minutes", "docId": 200},
                                             {"sourceType": "upload", "docId": 200}],
                  "query": "1분기 예산은 얼마로 하기로 했어?", "topK": 5}
DOCS_SCHEMA = {"type": "array", "maxItems": MAX_DOCS,
               "description": "볼 수 있는 문서. (sourceType, docId) 짝으로 구분하고 같은 짝은 하나로 본다. 비면 문서 검색을 하지 않는다",
               "items": {"type": "object", "required": ["sourceType", "docId"], "properties": {
                   "sourceType": {"type": "string", "enum": ["minutes", "upload"]},
                   "docId": {"type": "integer", "minimum": 1}}}}
ANSWER_SCHEMA = {
    "type": "object",
    "required": ["workspaceId", "docs", "query"],
    "properties": {
        "workspaceId": {"type": "integer", "minimum": 1, "description": "소속 워크스페이스"},
        "docs": DOCS_SCHEMA,
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
CHAT_EXAMPLE = {
    "workspaceId": 1, "meetingId": 310, "userId": 42, "userName": "김하늘",
    "query": "그거 언제까지 하기로 했지?",
    "docs": [{"sourceType": "minutes", "docId": 200}, {"sourceType": "upload", "docId": 200}],
    "history": [{"role": "user", "text": "지난 회의에서 디자인 시안 수정은 누가 맡았어?"},
                {"role": "assistant", "text": "박서준 님이 맡았어요 [1]."}],
    "currentAgendaId": 102, "liveAgendas": [], "recentLines": [],
}
CHAT_SCHEMA = {
    "type": "object",
    "required": ["workspaceId", "meetingId", "userId", "query", "docs"],
    "properties": {
        "workspaceId": {"type": "integer", "minimum": 1, "description": "검색을 이 워크스페이스로 거른다"},
        "meetingId": {"type": "integer", "minimum": 1, "description": "진행 중인 회의 (로그 추적용)"},
        "userId": {"type": "integer", "minimum": 1, "description": "질문한 사용자 (로그에는 이 ID만)"},
        "userName": {"type": ["string", "null"], "maxLength": MAX_USER_NAME,
                     "description": "질문한 사용자의 표시 이름 (선택). 지금은 쓰지 않는다 (SCRUM-224·225)"},
        "query": {"type": "string", "maxLength": MAX_CHAT_QUERY, "description": "질문"},
        "docs": {**DOCS_SCHEMA, "description": "찾을 문서: 볼 수 있는 지난 회의록 전부와 이 회의의 사전 첨부 자료. "
                                                "(sourceType, docId) 짝, 최대 1,000개. 비면 문서 검색을 하지 않는다"},
        "history": {"type": "array", "maxItems": MAX_HISTORY, "description": "이 사용자의 이 회의 대화, 오래된 것부터",
                    "items": {"type": "object", "required": ["role", "text"], "properties": {
                        "role": {"type": "string", "enum": list(ROLES)},
                        "text": {"type": "string", "maxLength": MAX_HISTORY_TEXT}}}},
        "currentAgendaId": {"type": ["integer", "string", "null"], "description": "지금 논의 중인 안건 (SCRUM-224)"},
        "liveAgendas": {"type": "array", "maxItems": MAX_LIVE_AGENDAS,
                        "description": "안건 보드의 모든 안건 + 제목. 지금은 형식만 보고 쓰지 않는다 (SCRUM-224)"},
        "recentLines": {"type": "array", "maxItems": MAX_RECENT_LINES,
                        "description": f"최근 전사 줄 (글 합계 {MAX_RECENT_CHARS}자 이하). 지금은 형식만 보고 쓰지 않는다 (SCRUM-224)"},
    },
}
CHAT_RESULT_EXAMPLE = {
    "status": "answered", "basis": "sources",
    "answer": "디자인 시안 수정은 11월 20일까지 하기로 했어요 [1].",
    "citations": [{"ref": 1, "sourceType": "minutes", "docId": 200, "title": "10월 디자인 회의",
                   "text": "디자인 시안: 수정본은 11월 20일까지 공유",
                   "location": {"type": "section", "heading": "결정 사항", "label": "결정 사항"}}],
    "errorCode": None,
}


def _error_doc(description, error, detail):
    return {"description": description,
            "content": {"application/json": {"example": {"error": error, "detail": detail}}}}


ERR_400 = _error_doc("입력 오류", "INVALID_INPUT", "docId 는 양의 정수여야 함")
ERR_503 = _error_doc("DB 를 쓸 수 없음", "NOT_READY", "DB 를 쓸 수 없음")
DOC_ID = Path(description="문서 번호 (양의 정수)", examples=["200"])
SOURCE_TYPE = Path(description="문서 종류: minutes(회의록) / upload(첨부 자료)", examples=["minutes"])
WORKSPACE_ID = Path(description="워크스페이스 번호 (양의 정수)", examples=["1"])


def invalid(detail):
    return JSONResponse(status_code=400, content={"error": "INVALID_INPUT", "detail": detail})


def conflict(detail):
    return JSONResponse(status_code=409, content={"error": "CONFLICT", "detail": detail})


def not_ready(detail):
    return JSONResponse(status_code=503, content={"error": "NOT_READY", "detail": detail})


def verify_internal_request(request: Request):
    """서버 간 인증. 지금은 내부망으로만 열고 검사하지 않는다 (백엔드와 합의). 서비스 간 토큰이 정해지면 여기서 검사한다."""
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
    doc = replace(doc, meeting_started_at=meeting_time(minutes))      # 읽을 수 없으면 None (날짜만 비움, SCRUM-234)
    return IngestRequest(doc, callback_url, minutes=minutes, request_id=request_id)


def parse_answer(body):
    """답변 요청 → (workspaceId, docs, query, topK). docs 는 (sourceType, docId) 짝 목록. history 등 다른 필드는 무시한다."""
    if not isinstance(body, dict):
        raise InvalidInput("본문이 JSON 객체가 아님")
    workspace_id = _positive_int(body, "workspaceId")
    docs = parse_docs(body)
    query = _text(body, "query", MAX_QUERY)
    top_k = body.get("topK", DEFAULT_TOP_K)
    if not _is_positive_int(top_k) or top_k > MAX_TOP_K:
        raise InvalidInput(f"topK 는 1~{MAX_TOP_K} 정수여야 함")
    return workspace_id, docs, query, top_k


def parse_docs(body) -> list[tuple[str, int]]:
    """docs: [{sourceType, docId}] → 겹치지 않는 (sourceType, docId) 짝 목록 (받은 순서). 옛 docIds 는 받지 않는다."""
    docs = body.get("docs")
    if docs is None and "docIds" in body:
        raise InvalidInput("docIds 는 더 받지 않음. docs 에 {sourceType, docId} 짝으로 보낸다 (챗봇 명세 v0.2 9장)")
    if not isinstance(docs, list):
        raise InvalidInput("docs 는 배열이어야 함")
    if len(docs) > MAX_DOCS:
        raise InvalidInput(f"docs 는 {MAX_DOCS}개 이하여야 함")
    pairs = []
    for i, d in enumerate(docs):
        if not isinstance(d, dict) or d.get("sourceType") not in SOURCE_TYPES:
            raise InvalidInput(f"docs[{i}].sourceType 은 {' 또는 '.join(SOURCE_TYPES)} 이어야 함")
        if not _is_positive_int(d.get("docId")):
            raise InvalidInput(f"docs[{i}].docId 는 양의 정수여야 함")
        pairs.append((d["sourceType"], d["docId"]))
    return list(dict.fromkeys(pairs))


def parse_chat(body) -> ChatRequest:
    """챗봇 요청 (명세 3장). 표에 없는 필드와 칸은 무시한다.
    liveAgendas·recentLines 는 지금(SCRUM-223) 모양과 상한만 보고 쓰지 않는다. 칸 검사는 쓰기 시작할 때(SCRUM-224) 더한다."""
    if not isinstance(body, dict):
        raise InvalidInput("본문이 JSON 객체가 아님")
    workspace_id = _positive_int(body, "workspaceId")
    meeting_id = _positive_int(body, "meetingId")
    user_id = _positive_int(body, "userId")
    query = _text(body, "query", MAX_CHAT_QUERY)
    docs = parse_docs(body)

    user_name = body.get("userName")
    if user_name is not None and (not isinstance(user_name, str) or len(user_name) > MAX_USER_NAME):
        raise InvalidInput(f"userName 은 {MAX_USER_NAME}자 이하 문자열이어야 함")
    current = body.get("currentAgendaId")
    if current is not None and (isinstance(current, bool) or not isinstance(current, (int, str))):
        raise InvalidInput("currentAgendaId 는 정수·문자열 또는 null 이어야 함")

    history = _array(body, "history", MAX_HISTORY)
    turns = []
    for i, t in enumerate(history):
        if not isinstance(t, dict) or t.get("role") not in ROLES:
            raise InvalidInput(f"history[{i}].role 은 {' 또는 '.join(ROLES)} 이어야 함")
        if not isinstance(t.get("text"), str) or len(t["text"]) > MAX_HISTORY_TEXT:
            raise InvalidInput(f"history[{i}].text 는 {MAX_HISTORY_TEXT}자 이하 문자열이어야 함")
        turns.append(Turn(t["role"], t["text"]))

    agendas = _array(body, "liveAgendas", MAX_LIVE_AGENDAS)
    if any(not isinstance(a, dict) for a in agendas):
        raise InvalidInput("liveAgendas 의 항목은 객체여야 함")
    lines = _array(body, "recentLines", MAX_RECENT_LINES)
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict) or not isinstance(ln.get("seq"), int) or isinstance(ln.get("seq"), bool) \
                or not isinstance(ln.get("text"), str):
            raise InvalidInput(f"recentLines[{i}] 는 seq(정수)와 text(문자열)가 있는 객체여야 함")
    if sum(len(ln["text"]) for ln in lines) > MAX_RECENT_CHARS:
        raise InvalidInput(f"recentLines 의 글 합계는 {MAX_RECENT_CHARS}자 이하여야 함")

    return ChatRequest(workspace_id, meeting_id, user_id, query, tuple(docs), tuple(turns), user_name, current,
                       tuple(agendas), tuple(lines))


def _array(body, key, max_len):
    """없거나 null 이면 빈 목록."""
    v = body.get(key)
    if v is None:
        return []
    if not isinstance(v, list):
        raise InvalidInput(f"{key} 는 배열이어야 함")
    if len(v) > max_len:
        raise InvalidInput(f"{key} 는 {max_len}개 이하여야 함")
    return v


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


def _path_source(raw):
    if raw not in SOURCE_TYPES:
        raise InvalidInput(f"sourceType 은 {' 또는 '.join(SOURCE_TYPES)} 이어야 함")
    return raw


def _path_id(raw, name):
    if not raw.isdigit() or int(raw) <= 0:
        raise InvalidInput(f"{name} 는 양의 정수여야 함")
    return int(raw)


def make_router(pool, allowed_callback_hosts=None, request_id_header=REQUEST_ID_HEADER, prefix=PREFIX, answerer=None,
                upload: UploadSettings | None = None, chatter=None):
    """pool: psycopg_pool.ConnectionPool (autocommit 연결, vector 형 등록).
    answerer: answer.Answerer. 없으면 답변 주소는 503 으로 답한다.
    chatter: chat.Chatter. 없으면 챗봇 주소는 503 으로 답한다.
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
                             "example": {"sourceType": "minutes", "docId": 200, "docVersion": "2",
                                         "status": "accepted"}}}},
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
        log.info("문서 넣기 doc=%s/%s version=%s status=%s enqueue=%s ignored=%s request_id=%s",
                 req.doc.source_type, req.doc.doc_id, req.doc.doc_version, result.status, result.enqueue, result.ignored, rid)
        return JSONResponse(status_code=202, content={"sourceType": req.doc.source_type, "docId": req.doc.doc_id,
                                                      "docVersion": req.doc.doc_version,
                                                      "status": "ignored" if result.ignored else "accepted"})

    @router.get(
        "/documents/{source_type}/{doc_id}", summary="상태 조회",
        description="status: accepted(접수됨) / processing(처리 중) / ready(검색 가능) / failed(실패, errorCode 참고)",
        responses={200: {"description": "문서 상태", "content": {"application/json": {"example": STATUS_EXAMPLE}}},
                   400: ERR_400,
                   404: _error_doc("넣은 적 없는 문서", "NOT_FOUND", "넣은 적 없는 문서"),
                   503: ERR_503},
    )
    def get_document(source_type: str = SOURCE_TYPE, doc_id: str = DOC_ID):
        try:
            source, did = _path_source(source_type), _path_id(doc_id, "docId")
            with pool.connection() as conn:
                status = get_status(conn, source, did)
        except InvalidInput as e:
            return invalid(str(e))
        except db_down as e:
            log.error("상태 조회 DB 오류 doc=%s/%s error=%s", source_type, doc_id, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        if status is None:
            return JSONResponse(status_code=404, content={"error": "NOT_FOUND", "detail": "넣은 적 없는 문서"})
        status["status"] = API_STATUS.get(status["status"], status["status"])
        return status

    @router.delete("/documents/{source_type}/{doc_id}", summary="문서 지우기", status_code=204,
                   description="문서와 저장한 조각, 대기 중인 작업을 지운다. 없는 문서여도 204.",
                   responses={400: ERR_400, 503: ERR_503})
    def remove_document(request: Request, source_type: str = SOURCE_TYPE, doc_id: str = DOC_ID):
        try:
            source, did = _path_source(source_type), _path_id(doc_id, "docId")
            with pool.connection() as conn:
                delete_document(conn, source, did)
        except InvalidInput as e:
            return invalid(str(e))
        except db_down as e:
            log.error("문서 지우기 DB 오류 doc=%s/%s error=%s", source_type, doc_id, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")
        log.info("문서 지우기 doc=%s/%s request_id=%s", source, did, request_id_of(request))
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
        description="볼 수 있는 문서(docs, {sourceType, docId} 짝) 안에서 질문과 가까운 조각을 찾아, LLM 이 그 조각만 근거로 답한다. "
                    "답의 문장마다 근거 번호 [n] 이 붙고, citations 에 그 번호의 원문과 위치가 담긴다. "
                    "status: answered(답함) / no_result(근거 없음, LLM 을 부르지 않음) / failed(errorCode 참고). "
                    "세 경우 모두 200 이다. 지금 LLM 은 시험용 가짜라 답 앞에 [시험용 답변] 이 붙는다.",
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
            "schema": ANSWER_SCHEMA, "example": ANSWER_EXAMPLE}}}},
        responses={200: {"description": "답변 결과 (status 로 구분)", "content": {"application/json": {
                       "example": ANSWER_RESULT_EXAMPLE}}},
                   400: _error_doc("입력 오류", "INVALID_INPUT", "docs[0].docId 는 양의 정수여야 함"),
                   503: _error_doc("DB·임베딩 모델·LLM 설정이 준비되지 않음", "NOT_READY", "DB 를 쓸 수 없음")},
    )
    def post_answer(request: Request, body: Any = Body(...)):
        rid = request_id_of(request)
        try:
            workspace_id, docs, query, top_k = parse_answer(body)
        except InvalidInput as e:
            log.info("답변 거부 request_id=%s reason=%s", rid, e)
            return invalid(str(e))
        reason = "답변 기능이 켜지지 않음" if answerer is None else answerer.not_ready_reason()
        if reason:
            log.error("답변 준비 안 됨 request_id=%s reason=%s", rid, reason)
            return not_ready(reason)
        try:
            return answerer.run(pool.connection, workspace_id, docs, query, top_k, request_id=rid)
        except db_down as e:
            log.error("답변 DB 오류 request_id=%s error=%s", rid, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")

    @router.post(
        "/chat", summary="회의 중 챗봇",
        description="회의 중 질문에 답한다 (명세 docs/api/rag-chat-contract.md). history 가 있으면 앞 대화를 보고 질문을 "
                    "고쳐 검색하고, 볼 수 있는 문서(docs, {sourceType, docId} 짝)의 조각만 근거로 답한다. 답의 문장마다 근거 번호 [n] 이 붙는다. "
                    "status: answered / no_result(answer 는 \"회의 자료에서 찾을 수 없어요\") / failed(errorCode 참고). "
                    "세 경우 모두 200 이고, 최대 60초 안에 답한다 (넘으면 failed + llm_timeout). 진행 중 회의(liveAgendas, recentLines)와 일반 지식은 아직 쓰지 않는다 "
                    "(SCRUM-224·225). 대화 기록은 저장하지 않으므로 요청마다 history 로 보낸다.",
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
            "schema": CHAT_SCHEMA, "example": CHAT_EXAMPLE}}}},
        responses={200: {"description": "챗봇 결과 (status 로 구분)", "content": {"application/json": {
                       "example": CHAT_RESULT_EXAMPLE}}},
                   400: _error_doc("입력 오류", "INVALID_INPUT", "history[0].role 은 user 또는 assistant 이어야 함"),
                   503: _error_doc("DB·임베딩 모델·LLM 설정이 준비되지 않음", "NOT_READY", "DB 를 쓸 수 없음")},
    )
    def post_chat(request: Request, body: Any = Body(...)):
        rid = request_id_of(request)
        try:
            req = parse_chat(body)
        except InvalidInput as e:
            log.info("챗봇 거부 request_id=%s reason=%s", rid, e)
            return invalid(str(e))
        reason = "챗봇 기능이 켜지지 않음" if chatter is None else chatter.not_ready_reason()
        if reason:
            log.error("챗봇 준비 안 됨 request_id=%s reason=%s", rid, reason)
            return not_ready(reason)
        try:
            return chatter.run(pool.connection, req, request_id=rid)
        except db_down as e:
            log.error("챗봇 DB 오류 request_id=%s error=%s", rid, type(e).__name__)
            return not_ready("DB 를 쓸 수 없음")

    return router
