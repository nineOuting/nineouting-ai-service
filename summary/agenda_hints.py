#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
먼저 알려드려요 - 용어 카드 (기능 추가 제안 1번의 1차 범위)

회의 중 낯선 용어가 처음 나오면 "이 말은 이런 뜻이에요" 카드를 만든다. 지난 회의 카드는 이 모듈에 넣지 않았다.
**서버에는 연결하지 않았다.** server/service.py, 보드 조립, 백엔드 계약은 건드리지 않고, 모듈과 자체 시험만 있다. 형식 제안은 docs/api/agenda-hints-contract.md.

카드를 만드는 길 두 가지
  1) 용어집(JSON)에 있는 말이 새 줄에 나오면 -> 모델 없이 카드를 만든다. origin = GLOSSARY, 뜻은 용어집 문장 그대로.
  2) 용어집에 없는 낯선 말 -> 모델을 불러 용어 후보와 한 줄 뜻을 받는다. origin = AI_EXPLAIN. 새 줄이 min_new_lines 개 모이거나
     refresh_sec(음성 시간)이 지나면 부른다. 코드가 후보를 확인해서 통과한 것만 카드로 만든다.

코드가 확인하는 것 (모델 출력을 믿지 않는다)
  - 용어가 근거로 낸 줄 안에 실제로 있어야 한다. STT 불확실 표시 안의 글자이면 안 된다 (잘못 받아쓴 말을 설명하지 않으려는 것).
  - confident 가 true 가 아니면 카드를 만들지 않는다 (오류로 세지 않는다).
  - 용어는 2~20자 한 줄, 뜻은 한 줄이고 60자 이하이며 「」 인용이 없어야 한다.
  - 이미 안내한 용어, 용어집에 있는 용어(용어집 길로 처리), 안건 제목에 든 말은 만들지 않는다.
  - 낮은 확신도 줄과 맞장구·간투사 줄은 용어를 찾는 입력에서 뺀다 (build_input 규칙).

카드 정책
  - 같은 용어는 회의 한 번에 한 번만 (사용자가 닫은 카드도 다시 만들지 않는다).
  - 동시에 보이는 카드는 최대 max_cards(3)장. 더 만들어지면 가장 오래된 카드는 목록에서 빠진다 (화면이 접는다).
  - 사용자가 "먼저 알려드려요"를 끄면(set_enabled(False)) 카드도 모델 호출도 없다. 다시 켜면 그 뒤 줄부터 본다.

호출 상한: 회의당 max_calls. 연속 3번 실패하거나 상한에 닿으면 모델 호출만 멈춘다 (용어집 길은 계속 된다).
줄은 한 번만 본다: 한 번 본 줄이 나중에 고쳐져도 다시 보지 않는다.
스레드: 일꾼 스레드(process, board)와 요청 스레드(dismiss, set_enabled, info)가 함께 쓴다. 상태 변경은 락 안에서, 모델 호출은 락 밖에서 한다.
로그 규칙: 전사 내용, 안건 제목, 용어, 뜻을 남기지 않는다. 개수와 예외 이름만 남긴다.
LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
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

from meeting_summary import call_llm, parse_llm_json, mmss, squash, build_input, build_char_stream  # noqa: E402

log = logging.getLogger("ai-service.agenda-hints")

TASK = "agenda_hints"                 # 토큰 사용량 지표의 ai.task 값 (새 값이라 cn 팀 집계 확인이 필요하다)
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 모델 호출을 멈춘다
KIND_TERM = "TERM"
ORIGIN_GLOSSARY = "GLOSSARY"
ORIGIN_AI = "AI_EXPLAIN"

SYSTEM_PROMPT = """너는 회의 전사에서 참석자가 낯설어할 만한 용어를 찾아 한 줄 뜻을 붙이는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어.

규칙
1. 전문용어, 약어, 외래어처럼 회의 밖 사람이 모를 수 있는 말만 골라. 일반 단어, 사람 이름, 회사·제품 이름, 숫자, 날짜는 고르지 마.
2. [이미 안내한 용어]와 [등록된 안건]에 있는 말은 고르지 마.
3. term은 전사에 나온 표기 그대로 써. 한 번에 최대 3개만, 정말 낯선 말만 골라. 없으면 빈 목록을 내.
4. term이 나온 줄 번호를 evidence에 [[시작, 끝], ...] 형태로 적어. 근거는 1개 이상이어야 해.
5. meaning은 그 말의 일반적인 뜻을 해요체 한 문장(60자 이내)으로 써. 회의 속 이름, 숫자, 결정을 넣지 마. 선택을 권하거나 편들지 마.
6. 뜻이 확실하지 않거나 STT가 잘못 받아쓴 말 같으면 confident를 false로 해.

출력은 JSON만. {"terms": [{"term": "리텐션", "meaning": "앱을 다시 찾는 사용자 비율이에요.", "evidence": [[2, 2]], "confident": true}]}"""


class HintsCfg:
    """용어 카드 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.min_new_lines = 4            # 새 줄이 이만큼 모이면 용어 후보를 찾는다 (의견·할 일과 같은 기준)
        self.refresh_sec = 60             # 새 줄이 있는데 이 시간(음성 시간)이 지났으면 모자라도 찾는다
        self.max_lines = 30               # 모델 한 번에 주는 줄 수
        self.max_calls = 60               # 회의당 호출 상한
        self.max_cards = 3                # 동시에 보이는 카드 수
        self.max_per_call = 3             # 한 번의 응답에서 받는 용어 수
        self.term_max_chars = 20
        self.explain_max_chars = 60
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def echo_llm(user):
    """모델을 부르지 않는 모의 응답. 후보 없음."""
    return json.dumps({"terms": []}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, SYSTEM_PROMPT, user, task=TASK)


def _norm(s):
    """용어 비교용 키: 공백·문장부호를 빼고 소문자로."""
    return _lower(squash(s))


def _lower(s):
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in s)


def _find_term(stream, mask, key):
    """소문자 글자열에서 key 가 처음 나오는 위치(STT 불확실 글자 밖). 영문·숫자 key 는 앞뒤가 영문·숫자이면 다른 말로 본다. 없으면 -1."""
    ascii_key = bool(re.fullmatch(r"[a-z0-9]+", key))
    start = 0
    while True:
        pos = stream.find(key, start)
        if pos < 0:
            return -1
        end = pos + len(key)
        ok = not any(mask[pos:end])
        if ok and ascii_key:
            before = stream[pos - 1] if pos > 0 else ""
            after = stream[end] if end < len(stream) else ""
            if re.match(r"[a-z0-9]", before) or re.match(r"[a-z0-9]", after):
                ok = False
        if ok:
            return pos
        start = pos + 1


# --------------------------------------------------------------------------------------
# 용어집
# --------------------------------------------------------------------------------------
class Glossary:
    """
    팀 용어집. 파일 형식(JSON):
      {"terms": [{"term": "리텐션", "aliases": ["retention"], "explain": "앱을 다시 찾는 사용자 비율이에요."}]}
    term 2~20자, explain 한 줄 60자 이하, 서로 겹치는 이름(term, aliases)이 없어야 한다. 어긋나면 ValueError.
    """
    TERM_MAX = 20
    EXPLAIN_MAX = 60

    def __init__(self, entries):
        self.entries = []
        self._by_key = {}
        for n, e in enumerate(entries, 1):
            where = f"terms[{n}]"
            if not isinstance(e, dict):
                raise ValueError(f"{where}: 객체가 아님")
            term, explain, aliases = e.get("term"), e.get("explain"), e.get("aliases", [])
            if not isinstance(term, str) or not 2 <= len(term.strip()) <= self.TERM_MAX or "\n" in term:
                raise ValueError(f"{where}: term 은 한 줄 2~{self.TERM_MAX}자 문자열이어야 함")
            if not isinstance(explain, str) or not explain.strip() or "\n" in explain or len(explain.strip()) > self.EXPLAIN_MAX:
                raise ValueError(f"{where}: explain 은 비어 있지 않은 한 줄 {self.EXPLAIN_MAX}자 이하 문자열이어야 함")
            if not isinstance(aliases, list) or not all(isinstance(a, str) and 2 <= len(a.strip()) <= self.TERM_MAX for a in aliases):
                raise ValueError(f"{where}: aliases 는 2~{self.TERM_MAX}자 문자열 목록이어야 함")
            entry = {"term": term.strip(), "explain": explain.strip(), "aliases": [a.strip() for a in aliases], "key": _norm(term)}
            for name in [term] + aliases:
                k = _norm(name)
                if k in self._by_key:
                    raise ValueError(f"{where}: 이름이 겹침 ({self._by_key[k]['term']})")
                self._by_key[k] = entry
            self.entries.append(entry)
        self.keys = sorted(self._by_key, key=len, reverse=True)       # 긴 이름부터 찾는다

    @classmethod
    def load(cls, path):
        """파일이 없거나 경로가 비어 있으면 빈 용어집. 형식이 틀리면 ValueError."""
        if not path or not os.path.exists(path):
            return cls([])
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"용어집 JSON 을 읽지 못함: {e.msg}") from None
        if not isinstance(data, dict) or not isinstance(data.get("terms"), list):
            raise ValueError("용어집 최상위는 {\"terms\": [...]} 여야 함")
        return cls(data["terms"])

    def find(self, key):
        return self._by_key.get(key)

    def __len__(self):
        return len(self.entries)


# --------------------------------------------------------------------------------------
class AgendaHints:
    def __init__(self, classifier, glossary=None, cfg=None, llm=None):
        self.c = classifier
        self.glossary = glossary or Glossary([])
        self.cfg = cfg or HintsCfg()
        self.llm = llm or make_llm(self.cfg)
        self.lock = threading.RLock()
        self.enabled = True
        self.ended = False
        self.seen = set()             # 이미 본 줄 id
        self.pending = []             # 용어 후보를 아직 찾지 않은 줄
        self.pending_since = 0        # pending 이 비어 있다가 채워진 음성 시간(ms)
        self.cards = []               # 만든 카드 전부 (순서대로): {"id","key","term","text","origin","evidence","dismissed"}
        self.shown = set()            # 카드를 만든 용어 키 (닫은 것도 포함)
        self.serial = 0
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.rejected = 0             # 코드 확인에서 떨어진 후보 수
        self.halted = None

    # ---------------- 입력 ----------------
    def _view(self):
        """분류기의 지금 상태를 복사해 온다 (분류기 락은 여기서만 잠깐 잡는다)."""
        c = self.c
        with c.lock:
            kept, _dropped, _text = build_input([dict(ln) for ln in c.store.snapshot()])
            titles = [a["title"] for a in c.agendas]
            now_ms = c.now_ms
        return {"kept": kept, "titles": titles, "now_ms": now_ms}

    def process(self, final=False):
        """새 줄에서 카드를 만든다. 용어집 길은 항상, 모델 길은 시점이 되면. 반환: 모델을 부른 횟수."""
        view = self._view()
        with self.lock:
            if final:
                self.ended = True
                self.pending = []
                return 0
            if self.ended:
                return 0
            new = [ln for ln in view["kept"] if ln["id"] not in self.seen]
            for ln in new:
                self.seen.add(ln["id"])
            if not self.enabled:
                self.pending = []
                return 0
            self._scan_glossary(new)
            if new and not self.pending:
                self.pending_since = view["now_ms"]
            self.pending += new
            job = self._llm_job(view)
        if job is None:
            return 0
        return self._run_llm(job)

    # ---------------- 용어집 길 ----------------
    def _scan_glossary(self, lines):
        if not len(self.glossary):
            return
        for ln in lines:
            stream, mask = build_char_stream([ln])
            stream = _lower(stream)
            found = []
            for key in self.glossary.keys:
                pos = _find_term(stream, mask, key)
                if pos >= 0:
                    found.append((pos, pos + len(key), key))
            # 더 긴 이름 안에 든 짧은 이름은 따로 세지 않는다 (리텐션율 안의 리텐션)
            found = [f for f in found if not any(o is not f and o[0] <= f[0] and f[1] <= o[1] and o[1] - o[0] > f[1] - f[0] for o in found)]
            for _pos, _end, key in sorted(found):
                entry = self.glossary.find(key)
                self._add_card(entry["key"], entry["term"], entry["explain"], ORIGIN_GLOSSARY, [ln["id"]])

    # ---------------- 모델 길 ----------------
    def _llm_job(self, view):
        """락 안에서 부른다. 지금 모델을 불러야 하면 일감, 아니면 None."""
        if not self.pending or self.halted:
            return None
        timed_out = view["now_ms"] - self.pending_since >= self.cfg.refresh_sec * 1000
        if len(self.pending) < self.cfg.min_new_lines and not timed_out:
            return None
        if self.calls >= self.cfg.max_calls:
            self.halted = "호출 상한"
            log.warning("호출 상한에 도달해 용어 후보 찾기를 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
            return None
        lines = self.pending[:self.cfg.max_lines]
        self.pending = self.pending[self.cfg.max_lines:]
        self.pending_since = view["now_ms"]
        told = [c["term"] for c in self.cards]
        user = self._user_text(told, view["titles"], lines)
        return {"lines": lines, "user": user, "titles": list(view["titles"])}

    @staticmethod
    def _user_text(told, titles, lines):
        return ("[이미 안내한 용어]\n" + ("\n".join(f"- {t}" for t in told) if told else "(없음)") +
                "\n\n[등록된 안건]\n" + ("\n".join(f"- {t}" for t in titles) if titles else "(없음)") +
                "\n\n[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {ln['text_marked']}" for k, ln in enumerate(lines, 1)))

    @staticmethod
    def _shape_error(d):
        if not isinstance(d, dict):
            return "최상위가 객체가 아님"
        if not isinstance(d.get("terms"), list):
            return "terms 가 목록이 아님"
        return None

    def _check_term(self, it, job):
        """후보 하나를 확인한다. 반환: (카드 재료 또는 None, 오류 문자열 또는 None). 오류 없이 None 이면 조용히 건너뛴 것(확신 낮음, 이미 안내함 등)."""
        if not isinstance(it, dict):
            return None, "후보가 객체가 아님"
        term, meaning, ev = it.get("term"), it.get("meaning"), it.get("evidence")
        if not isinstance(term, str) or not 2 <= len(term.strip()) <= self.cfg.term_max_chars or re.search(r"[\r\n]", term):
            return None, "term 은 한 줄 2~%d자 문자열이어야 함" % self.cfg.term_max_chars
        if it.get("confident") is not True:
            return None, None
        key = _norm(term)
        if key in self.shown or self.glossary.find(key) is not None:
            return None, None
        if any(key in _norm(t) for t in job["titles"]):
            return None, None
        if not isinstance(meaning, str) or not meaning.strip() or re.search(r"[\r\n]", meaning):
            return None, "meaning 은 비어 있지 않은 한 줄 문자열이어야 함"
        meaning = " ".join(meaning.split())
        if len(meaning) > self.cfg.explain_max_chars:
            return None, "meaning 이 너무 김"
        if "「" in meaning or "」" in meaning:
            return None, "meaning 에 인용(「」)이 있음"
        n = len(job["lines"])
        if not isinstance(ev, list) or not ev:
            return None, "근거(evidence)가 없음"
        spans = []
        for pair in ev:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(isinstance(x, int) for x in pair) and 1 <= pair[0] <= pair[1] <= n):
                return None, "근거 번호가 올바르지 않음"
            spans.append((pair[0], pair[1]))
        ids = sorted({job["lines"][k - 1]["id"] for a, b in spans for k in range(a, b + 1)})
        pool = [job["lines"][k - 1] for a, b in spans for k in range(a, b + 1)]
        stream, mask = build_char_stream(pool)
        if _find_term(_lower(stream), mask, key) < 0:
            return None, "term 이 근거 줄에 없거나 STT 불확실 글자임"
        return (term.strip(), meaning, ids), None

    def _run_llm(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur = job["user"], job["user"]
        d, calls = None, 0
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    break
                self.calls += 1
            calls += 1
            try:
                raw = self.llm(cur)
            except Exception as e:                               # 키 오류, 네트워크 오류, 사용량 제한 등
                with self.lock:
                    self._fail(type(e).__name__)
                break
            try:
                d = parse_llm_json(raw)
                err = self._shape_error(d)
            except Exception as e:
                d, err = None, f"JSON 해석 실패({type(e).__name__})"
            if err is None:
                break
            d = None
            if attempt == 2:
                with self.lock:
                    self._fail("BadOutput")
                break
            cur = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\n{err}. 형식에 맞게 JSON만 다시 출력해."
        if d is None:
            return calls
        with self.lock:
            self.err_streak = 0
            if not self.enabled or self.ended:
                return calls
            for it in d["terms"][:self.cfg.max_per_call]:
                card, err = self._check_term(it, job)
                if err:
                    self.rejected += 1
                elif card:
                    term, meaning, ids = card
                    self._add_card(_norm(term), term, meaning, ORIGIN_AI, ids)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("용어 후보 찾기를 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    # ---------------- 카드 ----------------
    def _add_card(self, key, term, text, origin, evidence):
        """락 안에서 부른다. 이미 안내한 용어(닫은 것 포함)는 만들지 않는다."""
        if key in self.shown:
            return
        self.shown.add(key)
        self.serial += 1
        self.cards.append({"id": f"h-{self.serial}", "key": key, "term": term, "text": text, "origin": origin,
                           "evidence": list(evidence), "dismissed": False})
        log.info("용어 카드 생성 meeting=%s origin=%s", self.c.meeting_id, origin)

    def board(self):
        """보드의 hints 칸. 닫지 않은 카드 중 가장 최근 max_cards 장. 꺼져 있으면 빈 목록."""
        with self.lock:
            if not self.enabled:
                return []
            live = [c for c in self.cards if not c["dismissed"]][-self.cfg.max_cards:]
            return [{"id": c["id"], "kind": KIND_TERM, "term": c["term"], "text": c["text"], "origin": c["origin"],
                     "evidenceSeqs": list(c["evidence"])} for c in live]

    # ---------------- 사람의 조작 ----------------
    def dismiss(self, card_id):
        """사용자가 카드를 닫았다. 반환: accepted / not_found. 닫은 용어는 다시 만들지 않는다."""
        with self.lock:
            for c in self.cards:
                if c["id"] == card_id:
                    c["dismissed"] = True
                    return "accepted"
            return "not_found"

    def set_enabled(self, on):
        with self.lock:
            self.enabled = bool(on)

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 용어, 뜻은 넣지 않는다."""
        with self.lock:
            return {"enabled": self.enabled, "cards": len(self.cards), "dismissed": sum(1 for c in self.cards if c["dismissed"]),
                    "fromGlossary": sum(1 for c in self.cards if c["origin"] == ORIGIN_GLOSSARY),
                    "fromAi": sum(1 for c in self.cards if c["origin"] == ORIGIN_AI),
                    "glossaryTerms": len(self.glossary), "pending": len(self.pending),
                    "calls": self.calls, "errors": self.errors, "rejected": self.rejected, "halted": self.halted}


# --------------------------------------------------------------------------------------
def selftest():
    import io
    import tempfile
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    A = [{"id": "a1", "key": "a1", "position": 1, "title": "배포 일정", "decision_point": "날짜 확정", "added": False}]
    GLOSS = Glossary([{"term": "리텐션", "aliases": ["retention"], "explain": "앱을 다시 찾는 사용자 비율이에요."},
                      {"term": "KPI", "explain": "목표 달성 정도를 재는 핵심 지표예요."},
                      {"term": "리텐션율", "explain": "일정 기간 뒤에도 남은 사용자 비율이에요."}])

    class World:
        def __init__(self, glossary=None, replies=None, **cfg):
            self.c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, max_lines=30))
            self.c.set_agendas(1, A)
            self.t, self.nid = 0, 0
            self.prompts, self.replies = [], list(replies or [])
            cfg.setdefault("min_new_lines", 4)
            self.h = AgendaHints(self.c, glossary if glossary is not None else Glossary([]), HintsCfg(**cfg), llm=self.fake)

        def fake(self, user):
            self.prompts.append(user)
            r = self.replies.pop(0) if self.replies else {"terms": []}
            if isinstance(r, Exception):
                raise r
            return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)

        def say(self, *texts, kind=None, low=False, gap=4000):
            rows = []
            for tx in texts:
                self.nid += 1
                rows.append({"id": self.nid, "start_ms": self.t, "end_ms": self.t + 1000, "arrival_ms": self.t + 1000, "type": "final",
                             "kind": kind, "low_confidence": low, "text": tx.replace("⟨", "").replace("⟩", ""), "text_marked": tx, "manual": False})
                self.t += gap
            self.c.receive({"lines": rows, "edits": [], "sent_ms": self.t})
            return self.h.process()

    def raises(exc, fn):
        try:
            fn()
        except exc:
            return
        raise SystemExit(f"{exc.__name__} 이 나야 하는데 안 남")

    def terms(w):
        return [c["term"] for c in w.h.board()]

    # 1) 용어집 파일: 읽기, 없는 파일, 틀린 형식
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "g.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"terms": [{"term": "온보딩", "explain": "처음 쓰는 사람이 서비스에 익숙해지는 과정이에요."}]}, f, ensure_ascii=False)
        assert len(Glossary.load(p)) == 1 and len(Glossary.load(os.path.join(d, "none.json"))) == 0 and len(Glossary.load("")) == 0
        for bad in ("{", "[]", '{"terms": 3}'):
            with open(p, "w", encoding="utf-8") as f:
                f.write(bad)
            raises(ValueError, lambda: Glossary.load(p))
    ok = {"term": "온보딩", "explain": "설명이에요."}
    for bad in ([{"term": "가", "explain": "x"}], [{"term": "온보딩", "explain": ""}], [{"term": "온보딩", "explain": "a\nb"}],
                [{"term": "온보딩", "explain": "가" * 61}], [{"term": "온보딩", "explain": "x", "aliases": "a"}],
                [ok, {"term": "온 보딩", "explain": "x"}], [ok, {"term": "다른말", "explain": "x", "aliases": ["온보딩"]}], ["문자열"], [{"explain": "x"}]):
        raises(ValueError, lambda: Glossary(bad))
    assert Glossary([{"term": "OKR", "explain": "x"}]).find(_norm("okr"))["term"] == "OKR"

    # 2) 용어집에 있는 말은 모델 없이 카드가 된다. 같은 말은 한 번만, 별칭도 같은 카드
    w = World(GLOSS)
    assert w.say("이번 분기 리텐션이 떨어졌어요") == 0 and w.prompts == []
    b = w.h.board()
    assert len(b) == 1 and b[0] == {"id": "h-1", "kind": "TERM", "term": "리텐션", "text": "앱을 다시 찾는 사용자 비율이에요.",
                                   "origin": "GLOSSARY", "evidenceSeqs": [1]}, b
    w.say("리텐션 얘기 다시 하면", "retention 도 같이 보죠")
    assert len(w.h.board()) == 1 and w.h.info()["fromGlossary"] == 1

    # 3) 영문 약어는 다른 단어 속에서 찾지 않는다. 한글은 조사가 붙어도 찾는다
    w = World(GLOSS)
    w.say("RAPID 팀이 모였어요", "KPIs 는 이상해요")
    assert terms(w) == [], terms(w)
    w.say("KPI가 중요해요")
    assert terms(w) == ["KPI"]

    # 4) 긴 이름 안에 든 짧은 이름은 따로 세지 않는다
    w = World(GLOSS)
    w.say("리텐션율을 봐야죠")
    assert terms(w) == ["리텐션율"], terms(w)

    # 5) STT 불확실 글자, 낮은 확신도 줄, 맞장구·간투사 줄에서는 카드를 만들지 않는다
    w = World(GLOSS)
    w.say("이번 ⟨리텐션⟩ 문제는요")
    w.say("리텐션 낮은 확신도", low=True)
    w.say("리텐션", kind="backchannel")
    w.say("리텐션", kind="filler")
    assert terms(w) == [] and w.h.info()["cards"] == 0
    w.say("KPI ⟨아닌⟩ 말")                                                # 마스크 밖이면 카드
    assert terms(w) == ["KPI"]

    # 6) 줄은 한 번만 본다: 고쳐진 줄을 다시 보지 않고, 같은 줄로 두 번 만들지 않는다
    w = World(GLOSS)
    w.say("KPI 얘기")
    snap = w.c.store.lines[1]
    snap["text"] = snap["text_marked"] = "리텐션 얘기"
    w.h.process()
    assert terms(w) == ["KPI"]

    # 7) 모델 길: 새 줄 4개가 모이면 부른다. 3개일 때는 부르지 않는다
    reply = {"terms": [{"term": "코호트", "meaning": "비슷한 조건의 사용자 묶음이에요.", "evidence": [[2, 2]], "confident": True}]}
    w = World(replies=[reply])
    assert w.say("첫 줄", "코호트 분석을 해야", "셋째") == 0 and w.prompts == [] and w.h.info()["pending"] == 3
    assert w.say("넷째") == 1 and len(w.prompts) == 1 and w.h.info()["pending"] == 0
    b = w.h.board()
    assert b == [{"id": "h-1", "kind": "TERM", "term": "코호트", "text": "비슷한 조건의 사용자 묶음이에요.", "origin": "AI_EXPLAIN", "evidenceSeqs": [2]}], b
    pr = w.prompts[0]
    assert "[이미 안내한 용어]\n(없음)" in pr and "- 배포 일정" in pr and "[2] 00:04 코호트 분석을 해야" in pr, pr

    # 8) 시간이 지나면 줄이 모자라도 부른다 (음성 시간 기준)
    w = World(replies=[{"terms": []}], refresh_sec=60)
    w.say("한 줄")
    assert w.prompts == []
    w.c.advance(w.t + 60000)
    assert w.h.process() == 1 and len(w.prompts) == 1
    assert w.h.process() == 0

    # 9) 이미 안내한 용어는 다음 호출 입력에 실린다. 줄이 많으면 max_lines 만큼씩 나눠 부른다
    w = World(replies=[reply, {"terms": []}], max_lines=4)
    w.say("a", "코호트", "c", "d")
    w.say("e", "f", "g", "h")
    assert "- 코호트" in w.prompts[1], w.prompts[1]
    w = World(replies=[], max_lines=2, min_new_lines=2)
    w.say("a", "b", "c", "d", "e")
    assert len(w.prompts) == 1 and w.h.info()["pending"] == 3
    w.h.process()
    assert len(w.prompts) == 2 and w.h.info()["pending"] == 1

    # 10) 코드 확인: 하나씩 떨어진다
    def one(**kw):
        d = {"term": "코호트", "meaning": "비슷한 조건의 사용자 묶음이에요.", "evidence": [[2, 2]], "confident": True}
        d.update(kw)
        return {"terms": [d]}

    def tries(resp, glossary=None):
        w = World(glossary, replies=[resp])
        w.say("첫 줄", "코호트 분석", "셋째", "넷째")
        return w

    bad_cases = [
        one(evidence=[[3, 3]]),                                  # 용어가 근거 줄에 없음
        one(evidence=[[1, 1]]),
        one(evidence=[[2, 9]]), one(evidence=[[0, 2]]), one(evidence=[]), one(evidence=None), one(evidence=[[2]]), one(evidence=[["2", "2"]]),
        one(meaning="가" * 61), one(meaning=""), one(meaning=None), one(meaning="두 줄\n이에요"), one(meaning="「코호트」는 묶음이에요"),
        one(term="가"), one(term="가" * 21), one(term=None), one(term="코호\n트"),
        {"terms": ["문자열"]},
    ]
    for resp in bad_cases:
        w = tries(resp)
        assert terms(w) == [] and w.h.info()["rejected"] == 1, (resp, w.h.info())
    for resp in (one(confident=False), one(confident="true"), one(confident=None), {"terms": []}):
        w = tries(resp)
        assert terms(w) == [] and w.h.info()["rejected"] == 0, resp          # 조용히 건너뛴다: 오류로 세지 않는다
    w = tries(one(term="배포"))                                    # 안건 제목에 든 말
    assert terms(w) == [] and w.h.info()["rejected"] == 0
    w = tries(one(), glossary=Glossary([{"term": "코호트", "explain": "x"}]))        # 용어집에 있으면 용어집 길로만
    assert terms(w) == ["코호트"] and w.h.board()[0]["origin"] == "GLOSSARY"
    w = World(replies=[one(evidence=[[2, 2]])])
    w.say("첫 줄", "⟨코호트⟩ 분석", "셋째", "넷째")                  # 불확실 글자 안의 용어
    assert terms(w) == [] and w.h.info()["rejected"] == 1
    w = World(replies=[one(term="Cohort ", evidence=[[2, 2]])])       # 영문 용어는 대소문자 무시, 앞뒤 공백 제거
    w.say("첫 줄", "그 cohort 분석", "셋째", "넷째")
    assert terms(w) == ["Cohort"], terms(w)

    # 10b) 경계값: 글자 수 상한과 하한은 포함이다
    for tm, mean in (("퍼널", "가" * 60), ("가" * 20, "나")):
        w = World(replies=[one(term=tm, meaning=mean, evidence=[[2, 2]])])
        w.say("첫 줄", f"그 {tm} 분석", "셋째", "넷째")
        assert terms(w) == [tm] and w.h.info()["rejected"] == 0, (tm, w.h.info())
    Glossary([{"term": "가나", "explain": "가" * 60}, {"term": "가" * 20, "explain": "x"}])

    # 11) 한 번에 받는 용어 수 상한, 한 응답 안의 중복
    three = {"terms": [dict(one()["terms"][0], term=t, evidence=[[2, 2]]) for t in ("코호트", "퍼널", "ARPU")]}
    w = World(replies=[three], max_per_call=2)
    w.say("첫 줄", "코호트 퍼널 ARPU 얘기", "셋째", "넷째")
    assert terms(w) == ["코호트", "퍼널"], terms(w)
    w = World(replies=[{"terms": [one()["terms"][0], one()["terms"][0]]}])
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert terms(w) == ["코호트"]

    # 12) JSON 이 깨지면 오류를 알려 한 번 더 부른다. 두 번 다 깨지면 실패로 센다
    w = World(replies=["이건 JSON 이 아님", reply])
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert len(w.prompts) == 2 and "[오류]" in w.prompts[1] and terms(w) == ["코호트"] and w.h.info()["errors"] == 0
    w = World(replies=['{"terms": 3}', '{"x": 1}'])
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert len(w.prompts) == 2 and w.h.info()["errors"] == 1 and terms(w) == []

    # 13) 호출이 예외로 실패하면 세고, 연속 3번이면 모델 호출을 멈춘다. 용어집 길은 계속 된다. 성공하면 연속 횟수가 0이 된다
    reply1 = one(evidence=[[1, 1]])
    w = World(GLOSS, replies=[RuntimeError("x"), RuntimeError("x"), reply1, RuntimeError("x"), RuntimeError("x"), RuntimeError("x")], min_new_lines=1)
    w.say("a"); w.say("b")
    assert w.h.info()["errors"] == 2 and w.h.halted is None
    w.say("코호트")                                                  # 성공: 연속 실패가 0으로
    assert w.h.info()["errors"] == 2 and terms(w) == ["코호트"] and w.h.halted is None
    for _ in range(3):
        w.say("x")
    assert w.h.halted == "연속 실패" and w.h.info()["errors"] == 5
    n = len(w.prompts)
    w.say("리텐션 얘기", "또 한 줄")
    assert len(w.prompts) == n and "리텐션" in terms(w)

    # 14) 호출 상한
    w = World(replies=[{"terms": []}] * 5, max_calls=2, min_new_lines=1)
    for i in range(5):
        w.say(f"줄 {i}")
    assert len(w.prompts) == 2 and w.h.halted == "호출 상한" and w.h.info()["calls"] == 2

    # 15) 카드는 동시에 3장까지. 닫은 카드, 밀려난 카드는 다시 만들지 않는다
    g = Glossary([{"term": t, "explain": f"{t} 설명이에요."} for t in ("가나", "다라", "마바", "사아")])
    w = World(g)
    w.say("가나"); w.say("다라"); w.say("마바")
    assert terms(w) == ["가나", "다라", "마바"]
    w.say("사아")
    assert terms(w) == ["다라", "마바", "사아"]
    w.say("가나 다시")                                                # 밀려난 카드는 다시 만들지 않는다
    assert terms(w) == ["다라", "마바", "사아"] and w.h.info()["cards"] == 4
    assert w.h.dismiss("h-3") == "accepted" and w.h.dismiss("h-99") == "not_found"
    b = [c["id"] for c in w.h.board()]
    assert "h-3" not in b, b
    w.say("마바 다시")                                                # 닫은 카드도 다시 만들지 않는다
    assert "마바" not in terms(w) and w.h.info()["cards"] == 4 and w.h.info()["dismissed"] == 1

    # 16) 껐다 켜기: 꺼져 있는 동안은 카드도 호출도 없고, 그 사이 줄은 다시 보지 않는다
    w = World(GLOSS, replies=[reply], min_new_lines=1)
    w.h.set_enabled(False)
    w.say("리텐션 코호트")
    assert w.h.board() == [] and w.prompts == [] and w.h.info()["cards"] == 0
    w.h.set_enabled(True)
    w.h.process()
    assert terms(w) == [] and w.prompts == []
    w.say("KPI 보자")
    assert terms(w) == ["KPI"]
    w.h.set_enabled(False)
    assert w.h.board() == [] and w.h.info()["cards"] == 1                # 꺼지면 보이지 않지만 만든 기록은 남는다

    # 17) 회의가 끝나면 아무것도 만들지 않는다
    w = World(GLOSS, replies=[reply])
    w.say("첫 줄", "둘째", "셋째")
    assert w.h.process(final=True) == 0
    w.say("KPI", "코호트")
    assert w.prompts == [] and terms(w) == [] and w.h.info()["pending"] == 0

    # 18) 응답을 기다리는 사이에 꺼지면 카드를 만들지 않는다
    w = World(replies=[reply])
    orig = w.h.llm

    def off_then_answer(user):
        w.h.set_enabled(False)
        return orig(user)
    w.h.llm = off_then_answer
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert w.h.info()["cards"] == 0

    # 18b) 응답을 기다리는 사이에 회의가 끝나도 카드를 만들지 않는다
    w = World(replies=[reply])
    orig = w.h.llm

    def end_then_answer(user):
        w.h.process(final=True)
        return orig(user)
    w.h.llm = end_then_answer
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert w.h.info()["cards"] == 0

    # 18c) echo 모드는 모델을 부르지 않고 후보도 내지 않는다
    assert json.loads(make_llm(HintsCfg())("아무 입력")) == {"terms": []}
    c0 = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, max_lines=30))
    assert AgendaHints(c0).llm is echo_llm

    # 19) 알 수 없는 설정은 거부한다. info 는 내용을 담지 않는다
    raises(TypeError, lambda: HintsCfg(nope=1))
    w = World(GLOSS)
    w.say("리텐션 SECRETWORD 전사")
    assert "SECRETWORD" not in json.dumps(w.h.info(), ensure_ascii=False) and "리텐션" not in json.dumps(w.h.info(), ensure_ascii=False)
    assert set(w.h.info()) == {"enabled", "cards", "dismissed", "fromGlossary", "fromAi", "glossaryTerms", "pending", "calls", "errors", "rejected", "halted"}

    # 20) 로그에 전사·안건 제목·용어·뜻이 남지 않는다
    reply1 = one(evidence=[[1, 1]])
    buf = io.StringIO()
    hd = logging.StreamHandler(buf)
    hd.setLevel(logging.DEBUG)
    log.addHandler(hd)
    old_level = log.level
    log.setLevel(logging.DEBUG)
    try:
        w = World(GLOSS, replies=[RuntimeError("SECRETWORD"), RuntimeError("x"), RuntimeError("x"), reply1], min_new_lines=1)
        w.say("리텐션 비밀문장입니다"); w.say("코호트 분석 비밀"); w.say("셋째 비밀"); w.say("넷째 비밀")
    finally:
        log.removeHandler(hd)
        log.setLevel(old_level)
    out = buf.getvalue()
    assert out and "비밀" not in out and "리텐션" not in out and "코호트" not in out and "배포 일정" not in out and "SECRETWORD" not in out, out

    # 21) 처리와 요청이 동시에 들어와도 깨지지 않는다
    w = World(GLOSS, min_new_lines=1)
    errs = []

    def hammer():
        try:
            for i in range(300):
                w.h.board()
                w.h.dismiss(f"h-{i % 5}")
                w.h.info()
                w.h.set_enabled(i % 7 != 0)
        except Exception as e:                                   # pragma: no cover
            errs.append(e)
    th = threading.Thread(target=hammer)
    th.start()
    for i in range(150):
        w.say(f"리텐션 KPI 줄 {i}")
    th.join()
    assert not errs, errs

    print("용어 카드 자체 시험 통과 (용어집 파일 형식, 용어집 길, 별칭, 영문 경계, 긴 이름 우선, STT 불확실·낮은 확신도·맞장구, 줄 한 번만 보기, 모델 호출 시점, 후보 코드 확인과 경계값, 상한, JSON 재시도, 연속 실패, 호출 상한, 카드 3장·닫기·다시 만들지 않음, 끄기·켜기, 종료, 로그에 내용 없음, 동시 접근)")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
