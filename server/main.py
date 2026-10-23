# -*- coding: utf-8 -*-
"""
ai-service 수신 서버 (FastAPI)

실행(개발): uvicorn server.main:app --reload   (저장소 루트에서)
설정: 환경변수 (.env.example 참고)

주소는 임시다. 백엔드 초안이 정해지면 아래 상수 네 개만 바꾼다.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .service import Registry, ConfigError, AdapterError, BatchOutOfOrder, NotReady, cfg_from_env
from .publisher import Publisher
from .metrics import Metrics, default_readers
import meeting_summary                      # summary/ 는 service.py 가 경로에 넣어 둔다

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

EVENTS_PATH = "/internal/meetings/{meeting_id}/events"
BATCHES_PATH = "/internal/meetings/{meeting_id}/batches"
AGENDAS_PATH = "/internal/meetings/{meeting_id}/agendas"                    # 안건 목록 전체 (PUT)
CORRECTIONS_PATH = "/internal/meetings/{meeting_id}/agenda-corrections"    # 사람이 고친 분류 (POST)
SUMMARY_EDITS_PATH = "/internal/meetings/{meeting_id}/agenda-summary-edits"  # 사람이 고친 요약 문장 (POST)
ITEM_EDITS_PATH = "/internal/meetings/{meeting_id}/agenda-item-edits"        # 사람이 고친 의견·할 일 (POST)
OFFTOPIC_ACTIONS_PATH = "/internal/meetings/{meeting_id}/off-topic-actions"  # 새 안건 제안을 무시했다는 알림 (POST)
END_REQUESTS_PATH = "/internal/meetings/{meeting_id}/end-requests"           # 종료 버튼·이어서 논의하기 (POST)

metrics = Metrics(default_readers(os.environ))
meeting_summary.USAGE_HOOK = metrics.record_token_usage          # 모델을 부를 때마다 토큰 사용량을 지표로 남긴다
meeting_summary.OPERATION_HOOK = metrics.record_operation     # 호출 시간과 실패 종류를 지표로 남긴다
publisher = Publisher.from_env(os.environ)
registry = Registry(cfg_from_env(), publisher=publisher)          # 잘못된 설정이면 서버가 뜨지 않는다


@asynccontextmanager
async def lifespan(app):
    if rag_runtime:
        rag_runtime.start()                 # RAG: 연결 풀 열기, 작업자 스레드 시작
    yield
    registry.shutting_down = True           # 종료 중에는 503 으로 답해 백엔드가 나중에 다시 보내게 한다
    publisher.wait_idle(5.0)
    if rag_runtime:
        rag_runtime.stop()                  # RAG: 처리 중인 작업을 마치고 멈춤
    metrics.shutdown()


app = FastAPI(title="nineouting ai-service", lifespan=lifespan)

# RAG (rag/): RAG_DATABASE_URL 이 있을 때만 붙인다. 없으면 rag 를 불러오지도 않아 요약만 쓰는 환경은 그대로다
rag_runtime = None
if os.environ.get("RAG_DATABASE_URL"):
    from rag.runtime import setup as rag_setup
    rag_runtime = rag_setup(app, os.environ)


def invalid(detail):
    """형식 오류 응답: 400 + INVALID_INPUT (입력 형식 문서 7장)"""
    return JSONResponse(status_code=400, content={"error": "INVALID_INPUT", "detail": detail})


def not_ready(detail):
    """준비 안 됨: 503. 백엔드는 보낼 것을 남겨 두고 10초 뒤 다시 보낸다."""
    return JSONResponse(status_code=503, content={"error": "NOT_READY", "detail": detail})


def out_of_order(expected):
    """묶음 번호가 이어지지 않음: 409 + expectedBatch. 백엔드가 그 번호부터 다시 보낸다."""
    return JSONResponse(status_code=409, content={"expectedBatch": expected})


@app.exception_handler(RequestValidationError)
def on_bad_body(request, exc):
    return invalid("요청 본문이 JSON 객체가 아님")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def get_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post(EVENTS_PATH)
def post_event(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_event(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(BATCHES_PATH)
def post_batch(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_batch(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except BatchOutOfOrder as e:
        return out_of_order(e.expected)
    except AdapterError as e:
        return invalid(str(e))


@app.put(AGENDAS_PATH)
def put_agendas(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_agendas(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(CORRECTIONS_PATH)
def post_corrections(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_corrections(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(SUMMARY_EDITS_PATH)
def post_summary_edits(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_summary_edits(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(ITEM_EDITS_PATH)
def post_item_edits(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_item_edits(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(OFFTOPIC_ACTIONS_PATH)
def post_offtopic_action(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_offtopic_action(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.post(END_REQUESTS_PATH)
def post_end_request(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_end_request(meeting_id, msg)
    except NotReady as e:
        return not_ready(str(e))
    except AdapterError as e:
        return invalid(str(e))


@app.get("/internal/meetings/{meeting_id}/status")
def get_status(meeting_id: str):
    s = registry.status(meeting_id)
    if s is None:
        raise HTTPException(status_code=404, detail="알 수 없는 회의")
    return s
