# -*- coding: utf-8 -*-
"""
ai-service 수신 서버 (FastAPI)

실행(개발): uvicorn server.main:app --reload   (저장소 루트에서)
설정: 환경변수 (.env.example 참고)

주소는 임시다. 백엔드 초안이 정해지면 아래 두 상수만 바꾼다.
"""
import logging
import os

from fastapi import Body, FastAPI, HTTPException

from .service import Registry, ConfigError, AdapterError, cfg_from_env

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

EVENTS_PATH = "/internal/meetings/{meeting_id}/events"
BATCHES_PATH = "/internal/meetings/{meeting_id}/batches"

app = FastAPI(title="nineouting ai-service")
registry = Registry(cfg_from_env())          # 잘못된 설정이면 서버가 뜨지 않는다


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post(EVENTS_PATH, status_code=202)
def post_event(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_event(meeting_id, msg)
    except AdapterError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post(BATCHES_PATH, status_code=202)
def post_batch(meeting_id: str, msg: dict = Body(...)):
    try:
        return registry.receive_batch(meeting_id, msg)
    except AdapterError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/internal/meetings/{meeting_id}/status")
def get_status(meeting_id: str):
    s = registry.status(meeting_id)
    if s is None:
        raise HTTPException(status_code=404, detail="알 수 없는 회의")
    return s
