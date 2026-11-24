# -*- coding: utf-8 -*-
"""
모델 호출 전체 동시 수 상한 (동시 회의 대비, SCRUM-288)

회의 하나가 모델을 동시에 최대 5~6개 부른다(AGENDA_PARALLEL=1). 회의가 N개면 최대 5~6N개라서, OpenAI 의 분당 처리량 제한(429)에 닿을 수 있다.
이 모듈은 서버 프로세스 전체에서 "지금 나가 있는 회의 중 모델 호출" 수를 세고, 상한을 정하면 넘는 호출을 줄 세워 기다리게 한다.

  LLM_MAX_CONCURRENT        동시에 나가 있을 수 있는 호출 수. 0(기본)이면 상한 없음(예전 동작). 호출 수는 세어서 지표로 내보낸다
  LLM_QUEUE_TIMEOUT_SEC     상한 때문에 기다리는 최대 시간(초). 0(기본)이면 끝까지 기다린다. 넘으면 CallQueueTimeout (모델 실패가 아니다)
  LLM_QUEUE_WARN_SEC        이 시간 이상 기다린 호출은 경고 로그를 남긴다 (기본 10초). 내용 없이 작업 이름, 대기 시간, 수만 남긴다

적용 범위: 회의 진행 중 처리(요약, 안건 분류·상태·요약·의견·할 일·주제 변경, 전사 속 용어)만. RAG 답변과 회의록 초안은 요청 단위로 적게 나가고
사용자가 기다리는 호출이라 줄 세우지 않는다. (같은 OpenAI 키를 쓰므로 회의 쪽을 줄이면 전체 사용량도 같이 준다.)

기다리는 순서는 대체로 먼저 온 순서다 (파이썬 Semaphore 의 깨우는 순서). 회의별 공평함은 보장하지 않는다.
상한을 얼마로 둘지는 지표(in_flight, peak)로 DEV 에서 재 본 뒤 정한다. 상한을 켜기 전까지는 동작이 바뀌지 않는다.
"""
import contextlib
import logging
import threading
import time

log = logging.getLogger("ai-service.call_limit")


class CallQueueTimeout(Exception):
    """상한 때문에 기다린 시간이 LLM_QUEUE_TIMEOUT_SEC 를 넘었다. 모델이 실패한 것이 아니므로 폴백으로 넘기지 않는다."""


class CallLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._sem = None
        self.limit = 0                  # 0 이면 상한 없음
        self.queue_timeout_s = 0.0      # 0 이면 끝까지 기다림
        self.warn_wait_s = 10.0
        self.in_flight = 0              # 지금 나가 있는 호출
        self.waiting = 0                # 상한 때문에 기다리는 호출
        self.peak = 0                   # 서버를 켠 뒤 동시에 나가 있던 호출의 최대

    def configure(self, limit=0, queue_timeout_s=0.0, warn_wait_s=10.0):
        """설정을 바꾼다. 이미 나가 있는 호출은 예전 설정의 자리를 쓰고 끝난다."""
        with self._lock:
            self.limit = int(limit)
            self._sem = threading.BoundedSemaphore(self.limit) if self.limit > 0 else None
            self.queue_timeout_s = float(queue_timeout_s)
            self.warn_wait_s = float(warn_wait_s)

    def snapshot(self):
        with self._lock:
            return {"limit": self.limit, "inFlight": self.in_flight, "waiting": self.waiting, "peak": self.peak}

    @contextlib.contextmanager
    def slot(self, task=None):
        """모델을 부르는 동안 자리 하나를 쓴다. 상한이 없으면 세기만 한다."""
        sem, timeout = self._sem, self.queue_timeout_s
        if sem is not None:
            with self._lock:
                self.waiting += 1
            started = self._clock()
            try:
                got = sem.acquire(timeout=timeout or None)
            finally:
                with self._lock:
                    self.waiting -= 1
            waited = self._clock() - started
            if not got:
                log.warning("모델 호출 대기 시간 초과 task=%s waited=%.1f limit=%d", task, waited, self.limit)
                raise CallQueueTimeout(f"{waited:.0f}초 기다렸지만 자리가 나지 않음")
            if waited >= self.warn_wait_s:
                snap = self.snapshot()
                log.warning("모델 호출이 상한 때문에 기다림 task=%s waited=%.1f in_flight=%d waiting=%d limit=%d",
                            task, waited, snap["inFlight"], snap["waiting"], self.limit)
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            yield
        finally:
            with self._lock:
                self.in_flight -= 1
            if sem is not None:
                sem.release()


LIMITER = CallLimiter()        # 서버 프로세스 하나에 하나
