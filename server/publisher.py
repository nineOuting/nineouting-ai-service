# -*- coding: utf-8 -*-
"""
회의 중 요약을 백엔드로 보낸다 (PUT).

  PUT {BACKEND_BASE_URL}{LIVE_SUMMARY_PATH}   (기본 경로: /internal/meetings/{meeting_id}/live-summary)

규칙
  - 회의마다 가장 최신 것 하나만 보낸다. 보내는 중에 더 새 것이 생기면 옛것은 버린다.
  - 2xx 가 아니면 재시도 3회, 간격 1초·2초·4초(지수 백오프). 408·429·5xx·연결 오류만 재시도하고, 그 밖의 4xx 는 바로 포기한다.
  - BACKEND_BASE_URL 이 없으면 보내지 않는다 (개발·시험용).
  - 로그에는 회의 번호, version, 상태 코드만 남긴다. 요약 글은 남기지 않는다.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("ai-service.publisher")

DEFAULT_PATH = "/internal/meetings/{meeting_id}/live-summary"


class Publisher:
    def __init__(self, base_url, path=DEFAULT_PATH, timeout=5.0, backoffs=(1.0, 2.0, 4.0), sender=None, sleep=time.sleep):
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = bool(self.base_url)
        self.path = path
        self.timeout = timeout
        self.backoffs = tuple(backoffs)
        self.sender = sender                       # 시험용: fn(url, payload) -> (성공, 재시도 가능, 상태 코드)
        self.sleep = sleep
        self.cond = threading.Condition()
        self.pending = {}
        self.busy = False
        self.thread = None
        self.stats = {"sent": 0, "failed": 0, "retries": 0, "replaced": 0}

    @classmethod
    def from_env(cls, env):
        return cls(env.get("BACKEND_BASE_URL"), env.get("LIVE_SUMMARY_PATH") or DEFAULT_PATH)

    def submit(self, meeting_id, payload):
        if not self.enabled:
            return
        with self.cond:
            if meeting_id in self.pending:
                self.stats["replaced"] += 1
            self.pending[meeting_id] = payload
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="publisher", daemon=True)
                self.thread.start()
            self.cond.notify_all()

    def _run(self):
        while True:
            with self.cond:
                while not self.pending:
                    self.cond.wait()
                meeting_id = next(iter(self.pending))
                payload = self.pending.pop(meeting_id)
                self.busy = True
            try:
                self._deliver(meeting_id, payload)
            except Exception as e:                  # 보내기 실패가 일꾼을 멈추면 안 된다
                self.stats["failed"] += 1
                log.error("요약 전송 중 예외 meeting=%s error=%s", meeting_id, type(e).__name__)
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    def _deliver(self, meeting_id, payload):
        url = self.base_url + self.path.format(meeting_id=meeting_id)
        version = payload.get("version")
        for attempt in range(len(self.backoffs) + 1):
            ok, retryable, code = self._send(url, payload)
            if ok:
                self.stats["sent"] += 1
                log.info("요약 전송 성공 meeting=%s version=%s status=%s", meeting_id, version, code)
                return
            if not retryable or attempt == len(self.backoffs):
                self.stats["failed"] += 1
                log.error("요약 전송 실패 meeting=%s version=%s status=%s", meeting_id, version, code)
                return
            with self.cond:
                newer = meeting_id in self.pending
            if newer:                               # 더 새 요약이 기다리고 있으니 옛것은 다시 보내지 않는다
                self.stats["replaced"] += 1
                log.warning("옛 요약 재시도 중단 meeting=%s version=%s", meeting_id, version)
                return
            self.stats["retries"] += 1
            log.warning("요약 전송 재시도 meeting=%s version=%s status=%s %s번째", meeting_id, version, code, attempt + 1)
            self.sleep(self.backoffs[attempt])

    def _send(self, url, payload):
        if self.sender:
            return self.sender(url, payload)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return 200 <= r.status < 300, False, r.status
        except urllib.error.HTTPError as e:
            return False, e.code >= 500 or e.code in (408, 429), e.code
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, True, None

    def wait_idle(self, timeout=10.0):
        """시험용: 보낼 것이 모두 끝날 때까지 기다린다."""
        with self.cond:
            return self.cond.wait_for(lambda: not self.pending and not self.busy, timeout)

    def info(self):
        return {"enabled": self.enabled, **self.stats}
