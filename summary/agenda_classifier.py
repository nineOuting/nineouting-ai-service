#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건 자동 분류 (AI-02)

전사 줄을 등록된 안건에 연결한다. 형식은 docs/api/agenda-contract.md, 예시는 examples/api/.

동작
  - 줄마다 안건 하나 또는 "해당 없음"(None)을 정한다. 직전 대화(이미 분류된 앞쪽 줄과 그 결과)를 함께 보여 준다.
  - 분류 대상은 요약과 같다 (맞장구·간투사·낮은 확신도 줄은 뺀다).
  - 안건 목록이 아직 없으면 줄을 모아 두고, 목록이 오면 분류한다.
  - 사람이 고친 줄은 고정한다 (자동 분류가 덮어쓰지 않고, 결과로도 보내지 않는다). 최근에 고친 몇 건은 모델에 예시로 보여 준다.
  - 안건이 목록에서 사라지면 그 안건의 줄은 해당 없음으로 돌린다.
  - 이미 분류한 줄은 안건이 바뀌거나 글이 고쳐져도 다시 분류하지 않는다.
  - 결과는 "새로 분류했거나 바뀐 줄"만 담는다 (take_result).

스레드: 요청 스레드(안건 목록, 고친 분류)와 일꾼 스레드(줄 처리, 모델 호출)가 함께 쓴다.
        모든 상태 변경은 락 안에서 하고, 모델 호출은 락 밖에서 한다.

로그 규칙: 전사 내용과 안건 제목을 남기지 않는다. 개수와 예외 이름만 남긴다.
모델 호출은 task="agenda_classification" 으로 토큰 지표에 남긴다. LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import json
import logging
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import TranscriptStore, build_input, call_llm, parse_llm_json, mmss  # noqa: E402

log = logging.getLogger("ai-service.classifier")

TASK = "agenda_classification"        # 토큰 사용량 지표의 ai.task 값
MAX_TRIES = 3                         # 모델이 한 줄을 끝내 답하지 않을 때 이만큼 시도하면 해당 없음으로 둔다
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 이 회의의 분류를 멈춘다
_MISSING = object()


class ClassifyCfg:
    """분류 설정 (기본값은 평가 뒤에 다시 정한다)."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.refresh_sec = 30         # 모델을 부르는 간격 (음성 시간 기준)
        self.max_calls = 200          # 회의당 모델 호출 상한 (넘으면 멈춤)
        self.max_lines = 30           # 한 번에 분류할 줄 수
        self.context_lines = 12       # 직전 대화로 보여 주는 줄 수
        self.examples = 5             # 사람이 고친 예를 보여 주는 최대 건수
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


SYSTEM_PROMPT = """너는 회의 발언을 등록된 안건에 연결하는 도구야. 입력은 STT가 만든 전사 줄이야.

규칙
1. [안건]에는 안건 번호, 제목, 결정할 사항이 있어.
2. [분류할 줄]의 줄마다 가장 알맞은 안건 번호를 하나 골라. 어느 안건과도 관계가 없으면 0이야.
3. 한 줄만 보지 말고 [직전 대화]의 흐름을 함께 봐. "그건 좋아요", "그럼 그렇게 하죠"처럼 앞 말을 받는 말은 앞 줄과 같은 안건으로 봐.
4. 안건의 제목이나 결정할 사항과 실제로 관련 있을 때만 그 안건으로 해. 억지로 연결하지 말고, 관련이 없는 잡담이나 다른 주제는 0으로 해.
5. [사용자가 고친 예]는 사용자가 정답으로 고친 것이야. 같은 기준을 따라.
6. 전사에 없는 내용을 지어내지 마. [분류할 줄]의 모든 줄에 하나씩 답해.

출력은 JSON만. {"lines": [{"n": 줄 번호, "agenda": 안건 번호 또는 0}]}"""


# --------------------------------------------------------------------------------------
# 모의 분류기 (모델을 부르지 않는다. 비용 0)
# --------------------------------------------------------------------------------------
def _clean(s):
    return " ".join(str(s).split())


def _tokens(s):
    return [t for t in re.findall(r"[0-9A-Za-z가-힣]+", s) if len(t) >= 2]


def _section(user, name):
    m = re.search(r"^\[%s\]\n(.*?)(?=\n\n\[|\Z)" % re.escape(name), user, re.S | re.M)
    return m.group(1) if m else ""


def echo_llm(user):
    """안건 제목·결정할 사항과 줄의 단어가 겹치는 정도로 고른다. 구조(번호, 개수)만 맞추는 모의 분류기."""
    agendas = {}
    for m in re.finditer(r"^\[(\d+)\] (.*?) / 결정할 사항: (.*)$", _section(user, "안건"), re.M):
        agendas[int(m.group(1))] = _tokens(m.group(2) + " " + m.group(3))
    out = []
    for m in re.finditer(r"^\[(\d+)\] \d\d:\d\d (.*)$", _section(user, "분류할 줄"), re.M):
        words = _tokens(m.group(2))
        best, best_score = 0, 0
        for num, toks in agendas.items():
            score = sum(1 for t in toks for w in words if w.startswith(t) or t.startswith(w))
            if score > best_score:
                best, best_score = num, score
        out.append({"n": int(m.group(1)), "agenda": best})
    return json.dumps({"lines": out}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, SYSTEM_PROMPT, user, task=TASK)


# --------------------------------------------------------------------------------------
# 분류기
# --------------------------------------------------------------------------------------
class AgendaClassifier:
    def __init__(self, meeting_id, cfg=None, llm=None):
        self.meeting_id = meeting_id
        self.cfg = cfg or ClassifyCfg()
        self.llm = llm or make_llm(self.cfg)
        self.lock = threading.RLock()
        self.store = TranscriptStore(meeting_id)
        self.agenda_version = None
        self.agendas = []             # [{"id", "key", "position", "title", "decision_point", "added"}] 순서대로
        self.keys = set()             # 지금 목록에 있는 안건의 key (문자열)
        self.orig = {}                # key -> 받은 값 그대로의 agendaId (결과를 보낼 때 같은 타입으로 돌려준다)
        self.labels = {}              # seq -> key 또는 None (자동 분류 결과)
        self.locks = {}               # seq -> {"key", "rev", "n"}  사람이 고쳐서 고정한 줄 (줄이 오기 전의 고침도 보관)
        self.lock_seq = 0
        self.sent = {}                # seq -> 마지막으로 내보낸 key 또는 None
        self.sent_current = None
        self.force = set()            # 안건 삭제로 값이 바뀐 고정 줄 (결과에 한 번 담는다)
        self.pending = set()          # 분류 대상이지만 아직 분류하지 못한 줄
        self.tries = {}
        self.now_ms = 0
        self.next_due_ms = self.cfg.refresh_sec * 1000
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.halted = None            # 멈춘 이유 (None이면 정상)

    # ---------------- 입력 ----------------
    def receive(self, batch):
        """어댑터를 거친 줄 묶음({"lines", "edits", "sent_ms"})을 받는다."""
        with self.lock:
            self.store.apply_batch(batch)
            if "sent_ms" in batch:
                self.now_ms = max(self.now_ms, batch["sent_ms"])
            self._scan()

    def advance(self, at_ms):
        with self.lock:
            self.now_ms = max(self.now_ms, at_ms)

    def _scan(self):
        kept, _dropped, _text = build_input(self.store.snapshot())
        ids = set()
        for ln in kept:
            i = ln["id"]
            ids.add(i)
            if i not in self.locks and i not in self.labels:
                self.pending.add(i)
        self.pending &= ids

    def set_agendas(self, version, agendas):
        """안건 목록 전체를 바꾼다. 반환: "accepted" 또는 "stale"(이미 받은 번호이거나 더 작음)."""
        with self.lock:
            if self.agenda_version is not None and version <= self.agenda_version:
                return "stale"
            new_keys = {a["key"] for a in agendas}
            removed = self.keys - new_keys
            self.agenda_version = version
            self.agendas = sorted(agendas, key=lambda a: a["position"])
            self.keys = new_keys
            for a in agendas:
                self.orig[a["key"]] = a["id"]
            for seq, k in self.labels.items():
                if k in removed:
                    self.labels[seq] = None
            for seq, lk in self.locks.items():
                if lk["key"] in removed:
                    lk["key"] = None
                    self.force.add(seq)
            return "accepted"

    def apply_corrections(self, corrections):
        """사람이 고친 분류를 고정한다. 반환: 새로 반영한 건수."""
        applied = 0
        with self.lock:
            for c in corrections:
                seq, rev = c["seq"], c["rev"]
                if seq in self.store.deleted:
                    continue
                cur = self.locks.get(seq)
                if cur is not None and rev <= cur["rev"]:
                    continue
                aid = c["agenda_id"]
                key = None if aid is None else str(aid)
                if key is not None:
                    self.orig.setdefault(key, aid)
                self.lock_seq += 1
                self.locks[seq] = {"key": key, "rev": rev, "n": self.lock_seq}
                self.pending.discard(seq)
                self.force.discard(seq)
                applied += 1
        return applied

    # ---------------- 분류 ----------------
    def _eff(self, seq):
        lk = self.locks.get(seq)
        return lk["key"] if lk is not None else self.labels.get(seq)

    def _wanted(self, final):
        if self.halted or not self.agendas or not self.pending:
            return False
        return final or len(self.pending) >= self.cfg.max_lines or self.now_ms >= self.next_due_ms

    def process(self, final=False):
        """분류할 때가 되었으면 모델을 불러 분류한다. 반환: 모델을 부른 횟수. 모델 호출은 락 밖에서 한다."""
        n = 0
        while True:
            with self.lock:
                job = self._prepare(final)
            if job is None:
                return n
            n += 1
            try:
                res = self._parse(self.llm(job["user"]), len(job["seqs"]), len(job["keys"]))
            except Exception as e:                      # 모델 호출 실패, JSON 해석 실패
                with self.lock:
                    self._fail(type(e).__name__)
                return n
            with self.lock:
                if not self._commit(job, res):
                    return n

    def _prepare(self, final):
        if not self._wanted(final):
            return None
        if self.calls >= self.cfg.max_calls:
            self.halted = "호출 상한"
            log.warning("분류 호출 상한에 도달해 멈춤 meeting=%s calls=%s", self.meeting_id, self.calls)
            return None
        snap = self.store.snapshot()
        by_id = {ln["id"]: ln for ln in snap}
        kept, _dropped, _text = build_input(snap)
        todo = [ln for ln in kept if ln["id"] in self.pending][: self.cfg.max_lines]
        if not todo:
            return None
        first = next(i for i, ln in enumerate(kept) if ln["id"] == todo[0]["id"])
        number = {a["key"]: n for n, a in enumerate(self.agendas, 1)}

        def tag(key):
            return f"안건 {number[key]}" if key in number else "해당 없음"

        context = [f"- {mmss(ln['start_ms'])} ({tag(self._eff(ln['id']))}) {_clean(ln['text'])}"
                   for ln in kept[max(0, first - self.cfg.context_lines): first] if ln["id"] not in self.pending]
        recent = sorted((lk["n"], seq) for seq, lk in self.locks.items()
                        if seq in by_id and (lk["key"] is None or lk["key"] in number))
        examples = [f"- \"{_clean(by_id[seq]['text'])[:80]}\" -> {tag(self.locks[seq]['key'])}"
                    for _n, seq in recent[-self.cfg.examples:]] if self.cfg.examples > 0 else []
        parts = ["[안건]\n" + "\n".join(
            f"[{n}] {_clean(a['title'])} / 결정할 사항: {_clean(a['decision_point'])}" for n, a in enumerate(self.agendas, 1))]
        if examples:
            parts.append("[사용자가 고친 예]\n" + "\n".join(examples))
        if context:
            parts.append("[직전 대화]\n" + "\n".join(context))
        parts.append("[분류할 줄]\n" + "\n".join(
            f"[{n}] {mmss(ln['start_ms'])} {_clean(ln['text'])}" for n, ln in enumerate(todo, 1)))
        self.calls += 1
        self.next_due_ms = self.now_ms + self.cfg.refresh_sec * 1000
        for ln in todo:
            self.tries[ln["id"]] = self.tries.get(ln["id"], 0) + 1
        return {"seqs": [ln["id"] for ln in todo], "keys": [a["key"] for a in self.agendas],
                "user": f"다음 {len(todo)}줄을 안건에 연결해줘.\n\n" + "\n\n".join(parts)}

    @staticmethod
    def _parse(raw, n_lines, n_agendas):
        """모델 응답 -> {줄 번호: 안건 번호(0은 해당 없음)}. 범위를 벗어났거나 형식이 틀린 항목은 버린다."""
        res = {}
        for it in parse_llm_json(raw).get("lines", []):
            n, a = it.get("n"), it.get("agenda")
            if (isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= n_lines
                    and isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= n_agendas):
                res[n] = a
        return res

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("분류 호출 실패 meeting=%s error=%s 연속=%s", self.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("분류를 멈춤 meeting=%s 연속 실패 %s번", self.meeting_id, self.err_streak)

    def _commit(self, job, res):
        if not res:
            self._fail("EmptyResult")
            return False
        self.err_streak = 0
        for n, seq in enumerate(job["seqs"], 1):
            if seq in self.locks or seq not in self.pending:      # 호출하는 사이 사람이 고쳤거나 없어진 줄
                continue
            if n in res:
                key = job["keys"][res[n] - 1] if res[n] else None
                if key is not None and key not in self.keys:      # 호출하는 사이 안건이 사라졌다: 다시 분류한다
                    continue
                self.labels[seq] = key
            elif self.tries.get(seq, 0) < MAX_TRIES:              # 모델이 답하지 않은 줄은 다음에 다시
                continue
            else:
                self.labels[seq] = None
            self.pending.discard(seq)
            self.tries.pop(seq, None)
        return True

    # ---------------- 출력 ----------------
    def _current_key(self):
        for ln in reversed(self.store.snapshot()):
            k = self._eff(ln["id"])
            if k is not None and k in self.keys:
                return k
        return None

    def _id(self, key):
        return None if key is None else self.orig.get(key, key)

    def take_result(self, final=False):
        """새로 분류했거나 바뀐 줄과 현재 안건. 보낼 것이 없으면 None (final이면 항상 돌려준다).
        돌려준 내용은 보낸 것으로 기록한다."""
        with self.lock:
            deleted = self.store.deleted
            rows = {}
            for seq, key in self.labels.items():
                if seq not in self.locks and seq not in deleted and self.sent.get(seq, _MISSING) != key:
                    rows[seq] = key
            for seq in self.force:
                if seq in self.locks and seq not in deleted:
                    rows[seq] = self.locks[seq]["key"]
            cur = self._current_key()
            if not rows and cur == self.sent_current and not final:
                return None
            self.sent.update(rows)
            self.sent_current = cur
            self.force.clear()
            return {"schemaVersion": "0.1", "meetingId": self.meeting_id, "atMs": self.now_ms, "final": bool(final),
                    "currentAgendaId": self._id(cur),
                    "lines": [{"seq": s, "agendaId": self._id(rows[s])} for s in sorted(rows)]}

    def info(self):
        """상태 조회용. 전사 내용과 안건 제목은 넣지 않는다."""
        with self.lock:
            return {"agendaVersion": self.agenda_version, "agendas": len(self.agendas),
                    "classified": len(self.labels), "pending": len(self.pending), "locked": len(self.locks),
                    "calls": self.calls, "errors": self.errors, "halted": self.halted,
                    "currentAgendaId": self._id(self._current_key())}


# --------------------------------------------------------------------------------------
def selftest():
    def ln(i, start, text, kind=None, low=False):
        return {"id": i, "start_ms": start, "end_ms": start + 1000, "arrival_ms": start + 1000, "type": "final",
                "kind": kind, "low_confidence": low, "text": text, "text_marked": text, "manual": False}

    def ag(i, title, decision, pos, key=None):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": decision, "added": False}

    A = [ag("a1", "배포 일정", "배포 날짜를 확정한다", 1), ag("a2", "예산 배분", "비용 한도를 정한다", 2)]
    fast = ClassifyCfg(refresh_sec=0, max_lines=30)

    def fresh(**kw):
        return AgendaClassifier("t", ClassifyCfg(**dict(refresh_sec=0, **kw)))

    # 1) 안건 목록이 없으면 줄을 모아 두고, 오면 분류한다. 맞장구·낮은 확신도 줄은 대상이 아니다
    c = fresh()
    c.receive({"lines": [ln(1, 0, "배포는 다음 주 목요일로 하죠"), ln(2, 4000, "네", kind="backchannel"),
                         ln(3, 8000, "비용 한도는 얼마로 할까요"), ln(4, 12000, "배포 일정 흐릿한 말", low=True)],
               "edits": [], "sent_ms": 13000})
    assert c.process() == 0 and c.take_result() is None and c.info()["pending"] == 2
    assert c.set_agendas(1, A) == "accepted" and c.set_agendas(1, A) == "stale" and c.set_agendas(0, A) == "stale"
    assert c.process() == 1
    r = c.take_result()
    assert r["lines"] == [{"seq": 1, "agendaId": "a1"}, {"seq": 3, "agendaId": "a2"}], r
    assert r["currentAgendaId"] == "a2" and r["final"] is False and r["schemaVersion"] == "0.1" and r["atMs"] == 13000
    assert c.take_result() is None, "같은 결과를 다시 내보냄"

    # 2) 증분: 새로 분류한 줄만 나온다. 해당 없음은 null. 사용자가 글을 고쳐 낮은 확신도가 풀리면 대상이 된다
    c.receive({"lines": [ln(5, 16000, "점심 뭐 먹을까요"), ln(6, 20000, "배포 서버를 정해야 해요")], "edits": [], "sent_ms": 21000})
    c.process()
    r = c.take_result()
    assert r["lines"] == [{"seq": 5, "agendaId": None}, {"seq": 6, "agendaId": "a1"}] and r["currentAgendaId"] == "a1", r
    c.receive({"lines": [], "edits": [{"id": 4, "rev": 1, "text": "배포 일정을 다시 보죠", "text_marked": "배포 일정을 다시 보죠"}], "sent_ms": 22000})
    c.process()
    assert c.take_result()["lines"] == [{"seq": 4, "agendaId": "a1"}]

    # 3) 사람이 고친 줄: 고정, 결과에 담지 않음, 낡은 rev 무시, 줄보다 먼저 온 고침은 보관, 삭제된 줄은 무시
    c = fresh()
    c.set_agendas(1, A)
    assert c.apply_corrections([{"seq": 9, "agenda_id": "a2", "rev": 1}]) == 1      # 줄 9는 아직 안 왔다
    c.receive({"lines": [ln(1, 0, "배포는 목요일로"), ln(9, 4000, "배포 관련 말인데 사실 예산 얘기"), ln(10, 8000, "배포 날짜 정하죠")],
               "edits": [{"id": 10, "rev": 1, "deleted": True}], "sent_ms": 9000})
    assert c.apply_corrections([{"seq": 10, "agenda_id": "a1", "rev": 1}]) == 0       # 삭제된 줄
    assert c.apply_corrections([{"seq": 9, "agenda_id": "a1", "rev": 1}]) == 0         # 같은 rev
    c.process()
    r = c.take_result()
    assert r["lines"] == [{"seq": 1, "agendaId": "a1"}], r                            # 9는 고정되어 안 나옴
    assert c.apply_corrections([{"seq": 1, "agenda_id": None, "rev": 2}]) == 1
    c.receive({"lines": [ln(11, 12000, "배포 일정 확인")], "edits": [], "sent_ms": 13000})
    c.process()
    assert c.take_result()["lines"] == [{"seq": 11, "agendaId": "a1"}]
    assert c.info()["locked"] == 2 and c.labels.get(9) is None and c._eff(9) == "a2" and c._eff(1) is None

    # 3-2) 분류한 뒤 결과를 내보내기 전에 사람이 고치면, 그 줄은 결과에 담지 않는다 (백엔드가 이미 고친 값을 갖고 있다)
    c = fresh()
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정"), ln(2, 4000, "예산 한도")], "edits": [], "sent_ms": 5000})
    c.process()
    assert c.apply_corrections([{"seq": 1, "agenda_id": "a2", "rev": 1}]) == 1
    assert c.take_result()["lines"] == [{"seq": 2, "agendaId": "a2"}]

    # 4) 모델에 주는 입력: 직전 대화(결과 포함)와 고친 예가 들어간다
    seen = []
    c = AgendaClassifier("t", fast, llm=lambda u: (seen.append(u) or echo_llm(u)))
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포는 목요일로 하죠"), ln(2, 4000, "그건 좋아요")], "edits": [], "sent_ms": 5000})
    c.process()
    c.apply_corrections([{"seq": 2, "agenda_id": "a2", "rev": 1}])
    c.receive({"lines": [ln(3, 8000, "예산 이야기")], "edits": [], "sent_ms": 9000})
    c.process()
    assert len(seen) == 2 and "[직전 대화]" not in seen[0] and "[사용자가 고친 예]" not in seen[0]
    assert "[사용자가 고친 예]\n- \"그건 좋아요\" -> 안건 2" in seen[1]
    assert "(안건 1) 배포는 목요일로 하죠" in seen[1] and "(안건 2) 그건 좋아요" in seen[1], seen[1]
    assert "[분류할 줄]\n[1] 00:08 예산 이야기" in seen[1]

    # 5) 안건이 사라지면 그 안건의 줄은 해당 없음으로 돌아가고, 고정한 줄도 한 번 결과에 담긴다
    c = fresh()
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정"), ln(2, 4000, "예산 한도"), ln(3, 8000, "예산 말인데")], "edits": [], "sent_ms": 9000})
    c.process()
    c.apply_corrections([{"seq": 3, "agenda_id": "a2", "rev": 1}])
    c.take_result()
    assert c.set_agendas(2, [A[0]]) == "accepted"
    r = c.take_result()
    assert r["lines"] == [{"seq": 2, "agendaId": None}, {"seq": 3, "agendaId": None}], r
    assert r["currentAgendaId"] == "a1" and c.take_result() is None
    # 5-2) 새 안건이 생겨도 이미 분류한 줄은 다시 분류하지 않는다
    assert c.set_agendas(3, [A[0], ag("a3", "예산 재검토", "한도 재확인", 2)]) == "accepted" and c.take_result() is None

    # 6) 호출하는 사이에 안건이 사라지면 그 결과는 버리고 다시 분류한다
    box = {}

    def racing(u):
        out = echo_llm(u)
        if "race" not in box:
            box["race"] = 1
            c6.set_agendas(2, [A[0]])              # 모델이 답하는 동안 안건 a2가 삭제됨
        return out
    c6 = AgendaClassifier("t", fast, llm=racing)
    c6.set_agendas(1, A)
    c6.receive({"lines": [ln(1, 0, "예산 한도 이야기")], "edits": [], "sent_ms": 1000})
    c6.process()
    assert c6.calls == 2 and c6.labels == {1: None} and c6.info()["pending"] == 0, (c6.calls, c6.labels)   # a2 결과는 버리고 다시 분류

    # 7) 실패와 이상한 응답: 줄은 남고, 연속 3번 실패하면 멈춘다. 모델이 일부 줄만 답하면 나머지는 다시 시도
    c = AgendaClassifier("t", fast, llm=lambda u: 1 / 0)
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정")], "edits": [], "sent_ms": 1000})
    for _ in range(5):
        c.process()
    i = c.info()
    assert i["errors"] == 3 and i["halted"] == "연속 실패" and i["pending"] == 1 and c.take_result() is None, i
    c = AgendaClassifier("t", fast, llm=lambda u: '{"lines": [{"n": 1, "agenda": 9}, {"n": "x", "agenda": 1}]}')
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정")], "edits": [], "sent_ms": 1000})
    c.process()
    assert c.info()["errors"] == 1 and c.info()["pending"] == 1, c.info()           # 범위를 벗어난 답은 버린다

    def skips_marked(u):                                                                  # "답없음"이 든 줄에는 끝내 답하지 않는 모델
        rows = re.findall(r"^\[(\d+)\] \d\d:\d\d (.*)$", _section(u, "분류할 줄"), re.M)
        return json.dumps({"lines": [{"n": int(n), "agenda": 1} for n, t in rows if "답없음" not in t]})
    c = AgendaClassifier("t", ClassifyCfg(refresh_sec=30), llm=skips_marked)              # 실제 간격(30초)으로
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정"), ln(2, 1000, "답없음 줄")], "edits": [], "sent_ms": 31000})
    assert c.process() == 1
    assert c.labels == {1: "a1"} and c.info()["pending"] == 1, c.labels                    # 답하지 않은 줄은 남겨 둔다
    for k in range(MAX_TRIES - 1):
        c.receive({"lines": [ln(3 + k, 2000 + k * 1000, "배포 일정 또 나옴")], "edits": [], "sent_ms": 62000 + k * 31000})
        assert c.process() == 1
    assert c.labels[2] is None and c.info()["pending"] == 0, (c.labels, c.info())          # 3번 시도하면 해당 없음
    assert c.info()["errors"] == 0 and c.info()["calls"] == MAX_TRIES

    # 8) 호출 상한, 한 번에 분류하는 줄 수, 종료
    c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, max_calls=2, max_lines=2))
    c.set_agendas(1, A)
    c.receive({"lines": [ln(i, i * 1000, "배포 일정") for i in range(1, 8)], "edits": [], "sent_ms": 8000})
    assert c.process() == 2 and c.process() == 0
    assert c.info()["halted"] == "호출 상한" and c.info()["calls"] == 2 and c.info()["pending"] == 3, c.info()
    c = AgendaClassifier("t", ClassifyCfg(refresh_sec=30))                              # 주기: 30초가 지나야 부른다
    c.set_agendas(1, A)
    c.receive({"lines": [ln(1, 0, "배포 일정")], "edits": [], "sent_ms": 10000})
    assert c.process() == 0
    c.advance(31000)
    assert c.process() == 1
    c.receive({"lines": [ln(2, 32000, "예산 한도")], "edits": [], "sent_ms": 33000})
    assert c.process() == 0 and c.process(final=True) == 1                               # 종료하면 남은 줄을 모두 분류
    r = c.take_result(final=True)
    assert r["final"] is True and [x["seq"] for x in r["lines"]] == [1, 2]
    assert fresh().take_result(final=True) == {"schemaVersion": "0.1", "meetingId": "t", "atMs": 0, "final": True,
                                                "currentAgendaId": None, "lines": []}   # 종료 결과는 비어도 낸다

    # 9) agendaId 타입은 받은 그대로 돌려준다 (정수 안건 번호)
    c = fresh()
    c.set_agendas(1, [{"id": 101, "key": "101", "position": 1, "title": "배포 일정", "decision_point": "날짜", "added": False}])
    c.receive({"lines": [ln(1, 0, "배포 일정")], "edits": [], "sent_ms": 1000})
    c.process()
    r = c.take_result()
    assert r["lines"] == [{"seq": 1, "agendaId": 101}] and r["currentAgendaId"] == 101 and type(r["currentAgendaId"]) is int
    assert c.apply_corrections([{"seq": 1, "agenda_id": 101, "rev": 1}]) == 1                 # 고침이 정수로 와도 같은 안건

    # 10) 토큰 지표의 작업 이름: 실제 호출 경로가 agenda_classification 을 넘긴다
    me = sys.modules[__name__]                  # 직접 실행해도 같은 모듈의 call_llm 을 바꾼다
    calls = []
    real = me.call_llm
    me.call_llm = lambda provider, model, system, user, mock_file=None, task=None: (
        calls.append((provider, model, task, system is SYSTEM_PROMPT)) or '{"lines": [{"n": 1, "agenda": 1}]}')
    try:
        c = AgendaClassifier("t", ClassifyCfg(provider="gemini", model="m", refresh_sec=0))
        c.set_agendas(1, A)
        c.receive({"lines": [ln(1, 0, "배포 일정")], "edits": [], "sent_ms": 1000})
        c.process()
    finally:
        me.call_llm = real
    assert calls == [("gemini", "m", "agenda_classification", True)], calls
    print("안건 분류기 자체 시험 통과 (안건 대기, 증분 결과, 고정과 예시, 직전 대화, 안건 삭제, 호출 중 변경, 실패, 상한, 종료, id 타입, 지표 이름)")


if __name__ == "__main__":
    selftest()
