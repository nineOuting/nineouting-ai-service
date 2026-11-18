#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
전사 속 용어 (REQ-191, 화면 기획서 Live_24)

회의 중 전사에 낯선 용어가 나오면 그 용어와 한 줄 뜻을 목록에 더한다. 화면은 이 목록으로 전사 전체에서 용어에 색을 칠하고,
마우스를 올리면 뜻을 보여 준다. 목록은 안건과 상관없이 따로 보낸다(server/service.py). 형식은 docs/api/transcript-terms-contract.md.

용어를 찾는 길 두 가지
  1) 용어집(JSON)에 있는 말이 새 줄에 나오면 -> 모델 없이 더한다. origin = GLOSSARY, 뜻은 용어집 문장 그대로.
  2) 용어집에 없는 낯선 말 -> 모델을 불러 용어 후보와 한 줄 뜻을 받는다. origin = AI_EXPLAIN. 새 줄이 min_new_lines 개 모이거나
     refresh_sec(음성 시간)이 지나면 부른다. 코드가 후보를 확인해서 통과한 것만 더한다.

코드가 확인하는 것 (모델 출력을 믿지 않는다)
  - 용어가 근거로 낸 줄 안에 실제로 있어야 한다. STT 불확실 표시 안의 글자이면 안 된다 (잘못 받아쓴 말을 설명하지 않으려는 것).
  - confident 가 true 가 아니면 더하지 않는다 (오류로 세지 않는다).
  - 용어는 2~20자 한 줄, 뜻은 한 줄이고 60자 이하이며 「」 인용이 없어야 한다.
  - 이미 찾은 용어, 용어집에 있는 용어(용어집 길로 처리), 안건 제목에 든 말은 더하지 않는다. 안건이 없으면 제목 검사만 건너뛴다.
  - 낮은 확신도 줄과 맞장구·간투사 줄은 용어를 찾는 입력에서 뺀다 (build_input 규칙).

목록 정책
  - 같은 용어(별칭 포함)는 회의 한 번에 한 번만 목록에 들어간다.
  - 목록은 오래된 것부터 최대 max_terms(200)개까지 남긴다. 넘으면 가장 오래된 것이 빠지고, 빠진 용어도 다시 더하지 않는다.

호출 상한: 회의당 max_calls. 연속 3번 실패하거나 상한에 닿으면 모델 호출만 멈춘다 (용어집 길은 계속 된다).
줄은 한 번만 본다: 한 번 본 줄이 나중에 고쳐져도 다시 보지 않는다.
스레드: 일꾼 스레드(process)와 요청 스레드(terms, info)가 함께 쓴다. 상태 변경은 락 안에서, 모델 호출은 락 밖에서 한다.
로그 규칙: 전사 내용, 안건 제목, 용어, 뜻을 남기지 않는다. 개수와 예외 이름만 남긴다.
LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import hashlib
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

log = logging.getLogger("ai-service.transcript-terms")

TASK = "transcript_terms"                 # 토큰 사용량 지표의 ai.task 값 (새 값이라 cn 팀 집계 확인이 필요하다)
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 모델 호출을 멈춘다
ORIGIN_GLOSSARY = "GLOSSARY"
ORIGIN_AI = "AI_EXPLAIN"

SYSTEM_PROMPT = """너는 회의 전사에서 참석자가 낯설어할 만한 용어를 찾아 한 줄 뜻을 붙이는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어.

규칙
1. 전문용어, 약어, 외래어, 기술·도구·서비스 이름(클라우드 플랫폼, 개발 도구 같은 것)처럼 회의 밖 사람이 모를 수 있는 말만 골라. 일반 단어, 사람 이름, 회사·기관 이름(그 회사를 가리킬 때), 숫자, 날짜는 고르지 마. 일상에서 흔히 쓰는 외래어(미팅, 피드백, 스케줄 같은 것)도 고르지 마.
2. [이미 안내한 용어]와 [등록된 안건]에 있는 말은 고르지 마.
3. term은 전사에 나온 표기 그대로 써. 한 번에 최대 3개만, 정말 낯선 말만 골라. 없으면 빈 목록을 내.
4. term이 나온 줄 번호를 evidence에 [[시작, 끝], ...] 형태로 적어. 근거는 1개 이상이어야 해.
5. meaning은 그 말의 일반적인 뜻을 해요체 한 문장(60자 이내)으로 써. 회의 속 이름, 숫자, 결정을 넣지 마. 선택을 권하거나 편들지 마.
6. 뜻이 확실하지 않거나 STT가 잘못 받아쓴 말 같으면 confident를 false로 해.

출력은 JSON만. {"terms": [{"term": "리텐션", "meaning": "앱을 다시 찾는 사용자 비율이에요.", "evidence": [[2, 2]], "confident": true}]}"""


class TermsCfg:
    """전사 속 용어 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.min_new_lines = 4            # 새 줄이 이만큼 모이면 용어 후보를 찾는다 (의견·할 일과 같은 기준)
        self.refresh_sec = 60             # 새 줄이 있는데 이 시간(음성 시간)이 지났으면 모자라도 찾는다
        self.max_lines = 30               # 모델 한 번에 주는 줄 수
        self.max_calls = 150              # 회의당 호출 상한 (회의 최대 1시간, 1시간 약 87~119번 추정에 여유)
        self.max_per_call = 3             # 한 번의 응답에서 받는 용어 수
        self.max_terms = 200              # 목록에 담는 용어 수 상한 (넘으면 가장 오래된 것부터 뺀다)
        self.term_max_chars = 20
        self.explain_max_chars = 60
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def term_id(key):
    """용어 id: 비교용 키(_norm, 용어집 용어는 대표 표기)의 해시 앞 12자리. 같은 용어면 회의가 달라도, 서버가 다시 시작해도 같다.
    로그에 찍혀도 용어 글자가 바로 보이지 않게 하려는 것이다 (같은 방식으로 해시해 맞춰 보면 알아낼 수 있으므로 암호는 아니다)."""
    return "t-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


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
    name(선택)은 화면에 보여 줄 이름으로 한 줄 2~40자다 (예: "리텐션 (Retention)"). 없으면 화면은 term 을 쓴다.
    """
    TERM_MAX = 20
    EXPLAIN_MAX = 60
    NAME_MAX = 40

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
            name = e.get("name")
            if name is not None and (not isinstance(name, str) or not 2 <= len(name.strip()) <= self.NAME_MAX or "\n" in name):
                raise ValueError(f"{where}: name 은 한 줄 2~{self.NAME_MAX}자 문자열이어야 함")
            entry = {"term": term.strip(), "explain": explain.strip(), "aliases": [a.strip() for a in aliases], "key": _norm(term),
                     "name": name.strip() if name else None}
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
class TranscriptTerms:
    def __init__(self, classifier, glossary=None, cfg=None, llm=None):
        self.c = classifier
        self.glossary = glossary or Glossary([])
        self.cfg = cfg or TermsCfg()
        self.llm = llm or make_llm(self.cfg)
        self.lock = threading.RLock()
        self.ended = False
        self.seen = set()             # 모델 길에서 이미 본 줄 id
        self.gl_seen = set()          # 용어집 길에서 이미 본 줄 id (용어집 찾기는 모델 길보다 먼저 따로 돌 수 있다)
        self.pending = []             # 용어 후보를 아직 찾지 않은 줄
        self.pending_since = 0        # pending 이 비어 있다가 채워진 음성 시간(ms)
        self.items = []               # 찾은 용어 전부 (순서대로): {"id","key","term","name","aliases","text","origin","first_seq"}
        self.known = set()            # 목록에 넣은 적 있는 용어 키 (목록에서 빠진 것도 포함)
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

    def scan_glossary(self):
        """용어집 길만 돈다 (모델 없음, 빠름). 서버는 줄 묶음을 분류기에 넣은 직후, 모델을 부르는 단계들보다 먼저 부른다.
        반환: 목록에 새로 넣은 용어 수."""
        view = self._view()
        with self.lock:
            if self.ended:
                return 0
            return self._glossary_new(view)

    def _glossary_new(self, view):
        """락 안에서 부른다. 용어집 길에서 아직 보지 않은 줄을 보고, 새로 넣은 용어 수를 돌려준다."""
        new = [ln for ln in view["kept"] if ln["id"] not in self.gl_seen]
        for ln in new:
            self.gl_seen.add(ln["id"])
        before = len(self.items)
        self._scan_glossary(new)
        return len(self.items) - before

    def process(self, final=False):
        """새 줄에서 용어를 찾는다. 용어집 길은 항상(scan_glossary 가 먼저 봤으면 건너뜀), 모델 길은 시점이 되면. 반환: 모델을 부른 횟수.
        final(회의 끝)이면 마지막 줄까지 용어집 길만 보고 모델은 부르지 않는다. 그 뒤로는 아무것도 하지 않는다."""
        view = self._view()
        with self.lock:
            if self.ended:
                return 0
            self._glossary_new(view)
            new = [ln for ln in view["kept"] if ln["id"] not in self.seen]
            for ln in new:
                self.seen.add(ln["id"])
            if final:
                self.ended = True
                self.pending = []
                return 0
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
                self._add(entry["key"], entry["term"], entry["explain"], ORIGIN_GLOSSARY, [ln["id"]],
                               name=entry["name"], aliases=entry["aliases"])

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
        told = [c["term"] for c in self.items]
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
        """후보 하나를 확인한다. 반환: (목록 재료 또는 None, 오류 문자열 또는 None). 오류 없이 None 이면 조용히 건너뛴 것(확신 낮음, 이미 안내함 등)."""
        if not isinstance(it, dict):
            return None, "후보가 객체가 아님"
        term, meaning, ev = it.get("term"), it.get("meaning"), it.get("evidence")
        if not isinstance(term, str) or not 2 <= len(term.strip()) <= self.cfg.term_max_chars or re.search(r"[\r\n]", term):
            return None, "term 은 한 줄 2~%d자 문자열이어야 함" % self.cfg.term_max_chars
        if it.get("confident") is not True:
            return None, None
        key = _norm(term)
        if key in self.known or self.glossary.find(key) is not None:
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
            if self.ended:
                return calls
            for it in d["terms"][:self.cfg.max_per_call]:
                card, err = self._check_term(it, job)
                if err:
                    self.rejected += 1
                elif card:
                    term, meaning, ids = card
                    self._add(_norm(term), term, meaning, ORIGIN_AI, ids)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("용어 후보 찾기를 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    # ---------------- 목록 ----------------
    def _add(self, key, term, text, origin, evidence, name=None, aliases=()):
        """락 안에서 부른다. 이미 찾은 용어(목록에서 빠진 것 포함)는 더하지 않는다. name(화면에 보일 이름)·aliases(같은 뜻의 다른 표기)는 용어집 길만 있다."""
        if key in self.known:
            return
        self.known.add(key)
        self.items.append({"id": term_id(key), "key": key, "term": term, "text": text, "origin": origin,
                           "first_seq": min(evidence), "name": name, "aliases": list(aliases)})
        log.info("용어 추가 meeting=%s origin=%s", self.c.meeting_id, origin)

    def terms(self):
        """이 회의에서 지금까지 찾은 용어 전부 (가장 오래된 것부터 max_terms 개까지 남김). 화면이 전사 전체에서 용어를 칠하고
        마우스를 올리면 뜻을 보여 주는 데 쓴다. 매번 새 목록을 만들어 돌려준다."""
        with self.lock:
            return [{"id": c["id"], "term": c["term"], **({"name": c["name"]} if c.get("name") else {}),
                     "aliases": list(c.get("aliases") or []), "text": c["text"], "origin": c["origin"],
                     "firstSeq": c["first_seq"]} for c in self.items[-self.cfg.max_terms:]]

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 용어, 뜻은 넣지 않는다."""
        with self.lock:
            return {"terms": len(self.items), "listed": min(len(self.items), self.cfg.max_terms),
                    "fromGlossary": sum(1 for c in self.items if c["origin"] == ORIGIN_GLOSSARY),
                    "fromAi": sum(1 for c in self.items if c["origin"] == ORIGIN_AI),
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
        def __init__(self, glossary=None, replies=None, agendas=A, **cfg):
            self.c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, max_lines=30))
            if agendas:
                self.c.set_agendas(1, agendas)
            self.t, self.nid = 0, 0
            self.prompts, self.replies = [], list(replies or [])
            cfg.setdefault("min_new_lines", 4)
            self.h = TranscriptTerms(self.c, glossary if glossary is not None else Glossary([]), TermsCfg(**cfg), llm=self.fake)

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
        return [c["term"] for c in w.h.terms()]

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

    # 2) 용어집에 있는 말은 모델 없이 목록에 들어간다. 같은 말은 한 번만, 별칭도 같은 용어
    w = World(GLOSS)
    assert w.say("이번 분기 리텐션이 떨어졌어요") == 0 and w.prompts == []
    b = w.h.terms()
    assert b == [{"id": term_id("리텐션"), "term": "리텐션", "aliases": ["retention"], "text": "앱을 다시 찾는 사용자 비율이에요.",
                  "origin": "GLOSSARY", "firstSeq": 1}], b
    w.say("리텐션 얘기 다시 하면", "retention 도 같이 보죠")
    assert len(w.h.terms()) == 1 and w.h.info()["fromGlossary"] == 1

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

    # 5) STT 불확실 글자, 낮은 확신도 줄, 맞장구·간투사 줄에서는 찾지 않는다
    w = World(GLOSS)
    w.say("이번 ⟨리텐션⟩ 문제는요")
    w.say("리텐션 낮은 확신도", low=True)
    w.say("리텐션", kind="backchannel")
    w.say("리텐션", kind="filler")
    assert terms(w) == [] and w.h.info()["terms"] == 0
    w.say("KPI ⟨아닌⟩ 말")                                                # 마스크 밖이면 찾는다
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
    b = w.h.terms()
    assert b == [{"id": term_id("코호트"), "term": "코호트", "aliases": [], "text": "비슷한 조건의 사용자 묶음이에요.", "origin": "AI_EXPLAIN", "firstSeq": 2}], b
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
    assert terms(w) == ["코호트"] and w.h.terms()[0]["origin"] == "GLOSSARY"
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

    # 15) 목록 상한: 넘으면 가장 오래된 것부터 빠지고, 빠진 용어는 다시 더하지 않는다
    g = Glossary([{"term": t, "explain": f"{t} 설명이에요."} for t in ("가나", "다라", "마바", "사아")])
    w = World(g, max_terms=3)
    w.say("가나"); w.say("다라"); w.say("마바")
    assert terms(w) == ["가나", "다라", "마바"]
    w.say("사아")
    assert terms(w) == ["다라", "마바", "사아"] and [x["id"] for x in w.h.terms()] == [term_id("다라"), term_id("마바"), term_id("사아")]
    w.say("가나 다시")                                                # 빠진 용어는 다시 더하지 않는다
    assert terms(w) == ["다라", "마바", "사아"] and w.h.info()["terms"] == 4 and w.h.info()["listed"] == 3

    # 16) 안건이 없는 회의에서도 두 길 모두 동작한다 (안건 제목 검사만 건너뛴다)
    w = World(GLOSS, replies=[one(term="배포", meaning="만든 것을 쓸 수 있게 내놓는 일이에요.", evidence=[[2, 2]])], agendas=None)
    w.say("리텐션 얘기", "배포 날짜", "셋째", "넷째")
    assert terms(w) == ["리텐션", "배포"], terms(w)
    assert "[등록된 안건]\n(없음)" in w.prompts[0], w.prompts[0]

    # 17) 회의가 끝나면: 마지막 줄까지 용어집 길만 보고 모델은 부르지 않는다. 그 뒤로는 아무것도 하지 않는다
    w = World(GLOSS, replies=[reply])
    w.say("첫 줄", "둘째", "셋째")
    w.c.receive({"lines": [{"id": 99, "start_ms": w.t, "end_ms": w.t + 1000, "arrival_ms": w.t + 1000, "type": "final", "kind": None,
                            "low_confidence": False, "text": "끝으로 KPI 코호트", "text_marked": "끝으로 KPI 코호트", "manual": False}],
                 "edits": [], "sent_ms": w.t + 1000})
    assert w.h.process(final=True) == 0
    assert w.prompts == [] and terms(w) == ["KPI"] and w.h.info()["pending"] == 0
    w.say("리텐션", "코호트", "a", "b")
    assert w.prompts == [] and terms(w) == ["KPI"]

    # 18) 응답을 기다리는 사이에 회의가 끝나면 모델 결과를 더하지 않는다
    w = World(replies=[reply])
    orig = w.h.llm

    def end_then_answer(user):
        w.h.process(final=True)
        return orig(user)
    w.h.llm = end_then_answer
    w.say("첫 줄", "코호트 분석", "셋째", "넷째")
    assert w.h.info()["terms"] == 0

    # 18c) echo 모드는 모델을 부르지 않고 후보도 내지 않는다
    assert json.loads(make_llm(TermsCfg())("아무 입력")) == {"terms": []}
    c0 = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, max_lines=30))
    assert TranscriptTerms(c0).llm is echo_llm

    # 19) 알 수 없는 설정은 거부한다. info 는 내용을 담지 않는다
    raises(TypeError, lambda: TermsCfg(nope=1))
    w = World(GLOSS)
    w.say("리텐션 SECRETWORD 전사")
    assert "SECRETWORD" not in json.dumps(w.h.info(), ensure_ascii=False) and "리텐션" not in json.dumps(w.h.info(), ensure_ascii=False)
    assert set(w.h.info()) == {"terms", "listed", "fromGlossary", "fromAi", "glossaryTerms", "pending", "calls", "errors", "rejected", "halted"}

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
            for _ in range(300):
                w.h.terms()
                w.h.info()
        except Exception as e:                                   # pragma: no cover
            errs.append(e)
    th = threading.Thread(target=hammer)
    th.start()
    for i in range(150):
        w.say(f"리텐션 KPI 줄 {i}")
    th.join()
    assert not errs, errs

    # 22) 목록 모양: 용어집 길은 이름·별칭을 담고, 모델 길은 별칭이 비어 있다. 돌려준 목록을 바꿔도 안쪽은 그대로다
    named = Glossary([{"term": "리텐션", "name": "리텐션 (Retention)", "aliases": ["retention"], "explain": "다시 찾는 사용자 비율이에요."},
                      {"term": "KPI", "explain": "목표 달성 정도를 재는 핵심 지표예요."}])
    w = World(named, replies=[{"terms": []}, {"terms": []},          # 앞의 두 줄에서는 모델 후보가 없다
                              {"terms": [{"term": "코호트", "meaning": "비슷한 조건의 사용자 묶음이에요.", "evidence": [[1, 1]], "confident": True}]}],
              min_new_lines=1)
    assert w.h.terms() == []
    w.say("리텐션이 중요해요")
    w.say("KPI 먼저 봐요")
    w.say("코호트로 나눠 볼까요")
    t = w.h.terms()
    assert [(x["id"], x["term"], x["origin"]) for x in t] == [(term_id("리텐션"), "리텐션", "GLOSSARY"), (term_id("kpi"), "KPI", "GLOSSARY"), (term_id("코호트"), "코호트", "AI_EXPLAIN")], t
    assert t[0]["name"] == "리텐션 (Retention)" and t[0]["aliases"] == ["retention"] and "name" not in t[1] and t[1]["aliases"] == [] and t[2]["aliases"] == []
    assert all(isinstance(x["firstSeq"], int) and x["text"] for x in t) and t[0]["firstSeq"] < t[1]["firstSeq"] < t[2]["firstSeq"]
    t[0]["aliases"].append("바뀜")
    t[0]["term"] = "바뀜"
    assert w.h.terms()[0]["term"] == "리텐션" and w.h.terms()[0]["aliases"] == ["retention"]
    w = World(Glossary([{"term": f"용어{i:02d}", "explain": "뜻이에요."} for i in range(5)]), min_new_lines=1, max_terms=3)
    for i in range(5):
        w.say(f"용어{i:02d} 이야기")
    assert [x["term"] for x in w.h.terms()] == ["용어02", "용어03", "용어04"], "상한을 넘으면 가장 오래된 것부터 뺀다"
    # 용어집 name 검사
    for bad in ({"term": "리텐션", "name": "가", "explain": "뜻이에요."}, {"term": "리텐션", "name": "가" * 41, "explain": "뜻이에요."},
                {"term": "리텐션", "name": 5, "explain": "뜻이에요."}, {"term": "리텐션", "name": "두 줄\n이름", "explain": "뜻이에요."}):
        try:
            Glossary([bad])
            raise AssertionError(f"잘못된 name 을 못 잡음: {bad}")
        except ValueError:
            pass
    assert Glossary([{"term": "리텐션", "name": "  리텐션 (Retention)  ", "explain": "뜻이에요."}]).entries[0]["name"] == "리텐션 (Retention)"

    # 23) id: 비교용 키의 해시. 모양은 t- + 16진수 12자리. 같은 용어면 회의가 달라도 같고, 별칭·대소문자·공백이 달라도 대표 표기의 id
    assert re.fullmatch(r"t-[0-9a-f]{12}", term_id("리텐션")) and term_id("리텐션") != term_id("kpi")
    w1, w2 = World(GLOSS), World(GLOSS)
    w1.say("리텐션 얘기")
    w2.say("retention 얘기")                                           # 다른 회의, 별칭으로 처음 나옴
    assert w1.h.terms()[0]["id"] == w2.h.terms()[0]["id"] == term_id("리텐션") and w2.h.terms()[0]["term"] == "리텐션"
    w3 = World(replies=[one(term="Cohort ", evidence=[[2, 2]])])
    w3.say("첫 줄", "그 cohort 분석", "셋째", "넷째")
    assert w3.h.terms()[0]["id"] == term_id("cohort")
    assert "리텐션" not in json.dumps([x["id"] for x in w1.h.terms()], ensure_ascii=False), "id 에 용어 글자가 그대로 보이면 안 됨"

    # 24) scan_glossary 가 먼저 돌아도: 용어집 용어는 바로 들어가고, 모델 길은 그 줄을 빠뜨리지 않는다
    w = World(GLOSS, replies=[reply])
    w.c.receive({"lines": [{"id": i, "start_ms": i * 4000, "end_ms": i * 4000 + 1000, "arrival_ms": i * 4000 + 1000, "type": "final", "kind": None,
                            "low_confidence": False, "text": tx, "text_marked": tx, "manual": False}
                           for i, tx in ((1, "리텐션 얘기"), (2, "코호트 분석을 해야"), (3, "셋째"), (4, "넷째"))], "edits": [], "sent_ms": 20000})
    assert w.h.scan_glossary() == 1 and terms(w) == ["리텐션"] and w.prompts == [], "용어집 찾기는 모델을 부르지 않는다"
    assert w.h.scan_glossary() == 0, "같은 줄을 두 번 보지 않는다"
    assert w.h.process() == 1 and terms(w) == ["리텐션", "코호트"] and "[2] 00:08 코호트 분석을 해야" in w.prompts[0], w.prompts
    w.h.process(final=True)
    assert w.h.scan_glossary() == 0

    print("전사 속 용어 자체 시험 통과 (용어집 파일 형식, 용어집 길, 별칭, 영문 경계, 긴 이름 우선, STT 불확실·낮은 확신도·맞장구, 줄 한 번만 보기, 모델 호출 시점, 후보 코드 확인과 경계값, 상한, JSON 재시도, 연속 실패, 호출 상한, 목록 상한·다시 더하지 않음, 안건 없는 회의, 종료, 로그에 내용 없음, 동시 접근, 목록 모양과 이름·별칭, 해시 id, 용어집 먼저 찾기)")


if __name__ == "__main__":
    if "--selftest" in sys.argv or len(sys.argv) == 1:
        selftest()
