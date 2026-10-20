# -*- coding: utf-8 -*-
"""
AI 서버가 만든 결과를 백엔드로 보낸다 (PUT).

  회의 중 요약   PUT {BACKEND_BASE_URL}{LIVE_SUMMARY_PATH}            (기본: /internal/meetings/{meeting_id}/live-summary)
  안건 분류 결과 PUT {BACKEND_BASE_URL}{AGENDA_CLASSIFICATION_PATH}   (기본: /internal/meetings/{meeting_id}/agenda-classification)
  안건 보드      PUT {BACKEND_BASE_URL}{AGENDA_BOARD_PATH}            (기본: /internal/meetings/{meeting_id}/agenda-board)

규칙
  - 회의마다, 종류마다 가장 최신 것 하나만 보낸다. 보내는 중에 더 새 것이 생기면 옛것은 버린다.
    단, 안건 분류 결과는 바뀐 줄만 담으므로 버리지 않고 줄 번호별 최신 값으로 합쳐서 보낸다 (merge_classification).
    안건 보드는 전체 상태라서 가장 큰 version 하나만 남긴다 (merge_latest). 같은 회의의 안건 보드는 1초에 한 번보다 자주 보내지 않는다.
  - 2xx 가 아니면 재시도 3회, 간격 1초·2초·4초(지수 백오프). 408·429·5xx·연결 오류만 재시도하고, 그 밖의 4xx 는 바로 포기한다.
  - 안건 분류 결과는 재시도를 다 써도 실패하면(5xx·연결 오류) 보관했다가 다음 결과에 합쳐 보낸다. 4xx 는 다시 보내도 소용없으므로 보관하지 않는다.
  - BACKEND_BASE_URL 이 없으면 보내지 않는다 (개발·시험용).
  - 로그에는 회의 번호, version, 상태 코드만 남긴다. 요약 글과 줄 내용은 남기지 않는다.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("ai-service.publisher")

DEFAULT_PATH = "/internal/meetings/{meeting_id}/live-summary"
CLASSIFICATION_PATH = "/internal/meetings/{meeting_id}/agenda-classification"
BOARD_PATH = "/internal/meetings/{meeting_id}/agenda-board"
LIVE_SUMMARY = "live_summary"
AGENDA_CLASSIFICATION = "agenda_classification"
AGENDA_BOARD = "agenda_board"
BOARD_MIN_INTERVAL_SEC = 1.0           # 연동 API 초안 7장: 안건 보드는 최대 초당 1번


def merge_classification(old, new):
    """안건 분류 결과 둘을 합친다. old 가 더 옛것이다. 같은 줄 번호는 new 의 값이 이긴다."""
    lines = {x["seq"]: x for x in old.get("lines", [])}
    lines.update({x["seq"]: x for x in new.get("lines", [])})
    out = dict(new)
    out["lines"] = [lines[s] for s in sorted(lines)]
    out["version"] = max(old.get("version", 0), new.get("version", 0))
    out["final"] = bool(old.get("final") or new.get("final"))
    return out


def merge_latest(old, new):
    """전체 상태를 보내는 종류: version 이 큰 쪽이 이긴다 (같으면 new)."""
    return new if new.get("version", 0) >= old.get("version", 0) else old


MERGERS = {AGENDA_CLASSIFICATION: merge_classification,      # 이 종류들은 못 보낸 것을 버리지 않고 보관해 다음에 합친다
           AGENDA_BOARD: merge_latest}
MIN_INTERVAL = {AGENDA_BOARD: BOARD_MIN_INTERVAL_SEC}


class Publisher:
    def __init__(self, base_url, path=DEFAULT_PATH, timeout=5.0, backoffs=(1.0, 2.0, 4.0), sender=None, sleep=time.sleep,
                 classification_path=CLASSIFICATION_PATH, board_path=BOARD_PATH, board_interval=BOARD_MIN_INTERVAL_SEC,
                 clock=time.monotonic):
        self.base_url = (base_url or "").rstrip("/")
        self.enabled = bool(self.base_url)
        self.paths = {LIVE_SUMMARY: path, AGENDA_CLASSIFICATION: classification_path or CLASSIFICATION_PATH,
                      AGENDA_BOARD: board_path or BOARD_PATH}
        self.intervals = dict(MIN_INTERVAL, **{AGENDA_BOARD: board_interval})
        self.clock = clock
        self.last_start = {}                       # (회의 번호, 종류) -> 마지막으로 보내기 시작한 시각
        self.timeout = timeout
        self.backoffs = tuple(backoffs)
        self.sender = sender                       # 시험용: fn(url, payload) -> (성공, 재시도 가능, 상태 코드)
        self.sleep = sleep
        self.cond = threading.Condition()
        self.pending = {}                          # (회의 번호, 종류) -> 보낼 내용
        self.carry = {}                            # (회의 번호, 종류) -> 보내지 못해 보관 중인 내용 (합치는 종류만)
        self.busy = False
        self.thread = None
        self.stats = {"sent": 0, "failed": 0, "retries": 0, "replaced": 0}

    @property
    def path(self):                                # 예전 이름 (회의 중 요약 경로)
        return self.paths[LIVE_SUMMARY]

    @classmethod
    def from_env(cls, env):
        return cls(env.get("BACKEND_BASE_URL"), env.get("LIVE_SUMMARY_PATH") or DEFAULT_PATH,
                   classification_path=env.get("AGENDA_CLASSIFICATION_PATH") or CLASSIFICATION_PATH,
                   board_path=env.get("AGENDA_BOARD_PATH") or BOARD_PATH)

    def submit(self, meeting_id, payload, kind=LIVE_SUMMARY):
        if not self.enabled:
            return
        key = (meeting_id, kind)
        merge = MERGERS.get(kind)
        with self.cond:
            if merge:
                older = self.carry.pop(key, None)             # 보내지 못해 보관한 것이 가장 옛것, 그다음 대기 중인 것
                if key in self.pending:
                    older = merge(older, self.pending[key]) if older else self.pending[key]
                if older:
                    payload = merge(older, payload)
            elif key in self.pending:
                self.stats["replaced"] += 1
            self.pending[key] = payload
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="publisher", daemon=True)
                self.thread.start()
            self.cond.notify_all()

    def _run(self):
        while True:
            with self.cond:
                while True:
                    while not self.pending:
                        self.cond.wait()
                    waits = {k: self._interval_wait(k) for k in self.pending}
                    key = next((k for k, w in waits.items() if w <= 0), None)
                    if key is not None:
                        break
                    self.cond.wait(min(waits.values()))       # 보내는 간격을 지키는 동안 더 새 내용이 오면 그것을 보낸다
                payload = self.pending.pop(key)
                self.last_start[key] = self.clock()
                self.busy = True
            try:
                self._deliver(key[0], key[1], payload)
            except Exception as e:                  # 보내기 실패가 일꾼을 멈추면 안 된다
                self.stats["failed"] += 1
                log.error("전송 중 예외 meeting=%s kind=%s error=%s", key[0], key[1], type(e).__name__)
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    def _interval_wait(self, key):
        interval, last = self.intervals.get(key[1], 0), self.last_start.get(key)
        if not interval or last is None:
            return 0.0
        return max(0.0, last + interval - self.clock())

    def _requeue(self, meeting_id, kind, payload):
        """합치는 종류는 못 보낸 내용을 버리지 않는다. 대기 중인 더 새 내용이 있으면 거기에 합치고, 없으면 보관한다."""
        merge = MERGERS.get(kind)
        if not merge:
            return
        key = (meeting_id, kind)
        with self.cond:
            if key in self.pending:
                self.pending[key] = merge(payload, self.pending[key])
            else:
                self.carry[key] = merge(self.carry[key], payload) if key in self.carry else payload

    def _deliver(self, meeting_id, kind, payload):
        url = self.base_url + self.paths[kind].format(meeting_id=meeting_id)
        version = payload.get("version")
        for attempt in range(len(self.backoffs) + 1):
            ok, retryable, code = self._send(url, payload)
            if ok:
                self.stats["sent"] += 1
                log.info("전송 성공 meeting=%s kind=%s version=%s status=%s", meeting_id, kind, version, code)
                return
            if not retryable or attempt == len(self.backoffs):
                self.stats["failed"] += 1
                log.error("전송 실패 meeting=%s kind=%s version=%s status=%s", meeting_id, kind, version, code)
                if retryable:                       # 4xx 는 다시 보내도 소용없으니 보관하지 않는다
                    self._requeue(meeting_id, kind, payload)
                return
            with self.cond:
                newer = (meeting_id, kind) in self.pending
            if newer:                               # 더 새 내용이 기다리고 있으니 옛것은 다시 보내지 않는다
                self.stats["replaced"] += 1
                log.warning("옛 내용 재시도 중단 meeting=%s kind=%s version=%s", meeting_id, kind, version)
                self._requeue(meeting_id, kind, payload)
                return
            self.stats["retries"] += 1
            log.warning("전송 재시도 meeting=%s kind=%s version=%s status=%s %s번째", meeting_id, kind, version, code, attempt + 1)
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
        with self.cond:
            carried = sum(len(p.get("lines", [])) for p in self.carry.values())
        return {"enabled": self.enabled, **self.stats, "carriedLines": carried}
