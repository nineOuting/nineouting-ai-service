#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 요약 (AI-03 현재 안건 요약)

안건마다 그 안건에 연결된 줄만 모아 2~3줄로 요약한다. 형식은 docs/api/agenda-summary-contract.md, 예시는 examples/api/.

동작
  - 요약 대상은 분류 결과가 그 안건인 줄이다 (해당 없음, 맞장구, 간투사, 낮은 확신도 줄은 뺀다. 사람이 고친 분류는 그대로 믿는다).
  - 갱신 때마다 그 안건의 원문 줄에서 새로 만든다. 앞서 만든 문장은 맥락으로만 보여 주고 근거로 쓰지 않게 한다.
  - 모든 문장은 근거 줄(evidenceSeqs)이 있어야 하고, 인용·숫자·STT 불확실 표시는 meeting_summary.validate_items 로 확인한다.
  - 사람이 고친 문장은 잠근다: 다시 만들지 않고, 검증기도 거치지 않는다.
    근거 줄의 글이 바뀌거나 사라졌을 때, 근거 줄이 다른 안건으로 옮겨 갔을 때만 needsCheck 를 켠다 (계약 6장 6번).
  - 갱신 시점: 새 줄이 min_new_lines 개 이상이면 바로, 1개면 refresh_sec(음성 시간) 뒤. 줄이 옮겨 갔거나 글이 바뀌었거나
    사람이 요약을 고친 안건은 기다리지 않는다. 회의가 끝나면(final) 바뀐 안건을 모두 한 번 더 요약한다.
  - 호출이 연속 3번 실패하거나 호출 상한에 닿으면 이 회의의 요약만 멈춘다 (분류는 영향받지 않는다).
  - 결과는 안건 보드의 전체 상태다 (take_board). 바뀐 것이 없으면 None.

스레드: 일꾼 스레드(process, take_board)와 요청 스레드(apply_edits)가 함께 쓴다. 상태 변경은 락 안에서 하고 모델 호출은 락 밖에서 한다.
        분류기의 락은 읽을 때만 잠깐 잡고(_view), 요약기의 락과 겹쳐 잡지 않는다.

로그 규칙: 전사 내용, 안건 제목, 요약 문장을 남기지 않는다. 개수와 예외 이름만 남긴다.
모델 호출은 task="agenda_summary" 로 토큰 지표에 남긴다. LLM_PROVIDER=echo 이면 모델을 부르지 않는다 (비용 0).
"""
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import (SYSTEM_PROMPT, build_input, validate_items, call_llm, parse_llm_json, mmss)  # noqa: E402
from meeting_live_summary_v2 import echo_llm, window_range  # noqa: E402
from agenda_classifier import _clean  # noqa: E402

log = logging.getLogger("ai-service.agenda-summary")

TASK = "agenda_summary"               # 토큰 사용량 지표의 ai.task 값
MAX_STREAK = 3                        # 연속 실패가 이만큼이면 이 회의의 요약을 멈춘다

AGENDA_ADDENDUM = """
18. 지금 주는 전사는 회의 전체가 아니라 하나의 안건에 연결된 발언만 모은 거야. 근거 줄 번호는 이 전사의 번호만 써.
19. [안건]의 제목과 결정할 사항은 이야기가 무엇에 관한 것인지 이해하는 데만 써. 전사에 없는 내용을 제목에서 가져와 쓰지 마.
20. [이전 요약]은 "그 역할", "아까 말한 것" 같은 말이 무엇을 가리키는지 이해하는 데만 써. 거기 있는 문장을 베끼거나 근거로 쓰지 마. 전사에 없는 내용은 쓰지 마.
21. [사람이 확정한 항목]이 있으면 그 항목이 다룬 내용은 다시 쓰지 말고, 나머지 내용만 항목으로 만들어.
22. 요약할 내용이 없으면 summary를 빈 목록으로 줘.
23. 전사의 ⟨ ⟩ 기호는 STT 불확실 글자를 표시한 것일 뿐이야. 「」 인용 안에는 ⟨ ⟩를 넣지 말고, 기호만 빼고 글자를 원문 그대로 옮겨.
24. STT 불확실 글자가 든 말을 「」로 인용하면 본문에 "STT 불확실"이라고 꼭 적어. 숫자는 뒤에 이어지는 글자까지 빼지 말고 끝까지 인용해.
25. 전사에 없는 명사나 해석(예: "제품", "유튜브")을 덧붙이지 마. 말한 내용만 옮겨."""

AGENDA_SYSTEM_PROMPT = SYSTEM_PROMPT.replace("\n\n출력은 JSON만.", AGENDA_ADDENDUM + "\n\n출력은 JSON만.")
assert AGENDA_SYSTEM_PROMPT != SYSTEM_PROMPT, "시스템 프롬프트에 추가 규칙을 끼워 넣지 못함 (meeting_summary.py 버전 확인)"


class SummarizeCfg:
    """안건 요약 설정. 값은 모두 제안이며 평가 뒤에 다시 정한다."""

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.refresh_sec = 60         # 새 줄이 1개뿐일 때 기다리는 시간 (음성 시간 기준)
        self.min_new_lines = 2        # 새 줄이 이만큼이면 기다리지 않고 바로 요약
        self.max_sentences = 3        # 안건당 문장 수 (사람이 고친 문장이 먼저 자리를 차지)
        self.max_calls = 300          # 회의당 모델 호출 상한 (비용을 추정한 뒤 정한다)
        self.max_lines = 200          # 한 번에 모델에 주는 줄 수 (넘으면 가장 최근 줄)
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, AGENDA_SYSTEM_PROMPT, user, task=TASK)


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:10]


def _new_state():
    return {"items": [], "reflected": {}, "wait_since": None, "dirty": False}


class AgendaSummarizer:
    def __init__(self, classifier, cfg=None, llm=None, id_prefix=None, now=time.time):
        self.c = classifier
        self.cfg = cfg or SummarizeCfg()
        self.llm = llm or make_llm(self.cfg)
        self.now = now
        self.lock = threading.RLock()
        # 문장 번호: 재시작해도 겹치지 않도록 시작 시각과 무작위 값을 섞는다
        self.prefix = id_prefix or (_b36(int(now() * 1000)) + secrets.token_hex(1))
        self.serial = 0
        self.ag = {}                  # 안건 key -> 상태 {"items", "reflected", "wait_since", "dirty"}
        self.known = set()            # 안건 목록에 한 번이라도 있었던 key (사라졌는지 가리는 데 쓴다)
        self.assign = {}              # seq -> 지난번에 본 안건 key ("" 이면 해당 없음). 줄이 옮겨 갔는지 알아내는 데 쓴다
        self.item_rev = {}            # 문장 번호 -> 마지막으로 반영한 rev
        self.version = 0
        self.sent_sig = None
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.halted = None            # 멈춘 이유 (None이면 정상)

    # ---------------- 분류기에서 읽기 ----------------
    def _view(self):
        """분류기의 지금 상태를 복사해 온다 (분류기 락은 여기서만 잠깐 잡는다)."""
        c = self.c
        with c.lock:
            snap = c.store.snapshot()
            kept, _dropped, _text = build_input(snap)
            decided = {ln["id"] for ln in snap if ln["id"] in c.locks or ln["id"] in c.labels}
            return {
                "agendas": [dict(a) for a in c.agendas],
                "keys": set(c.keys),
                "kept": [dict(ln) for ln in kept],
                "by_id": {ln["id"]: {"text": ln["text"], "start_ms": ln["start_ms"]} for ln in snap},
                "eff": {i: c._eff(i) for i in decided},          # 분류가 정해진 줄 -> 안건 key 또는 None
                "deleted": set(c.store.deleted),
                "now_ms": c.now_ms,
                "current": c._current_key(),
                "ids": {a["key"]: c._id(a["key"]) for a in c.agendas},
            }

    # ---------------- 사람이 고친 요약 ----------------
    def apply_edits(self, edits):
        """edits: [{"agenda_id", "item_id", "action": "edit"|"confirm", "text", "evidence_seqs", "rev"}] (어댑터를 거친 것).
        반환: {"applied": 새로 반영한 수, "ignored": [{"itemId", "reason"}]}  reason 은 "stale" 또는 "not_edited"."""
        with self.c.lock:
            for e in edits:
                self.c.orig.setdefault(str(e["agenda_id"]), e["agenda_id"])   # 안건 목록보다 먼저 와도 같은 타입으로 돌려주기 위해
        view = self._view()
        applied, ignored = 0, []
        with self.lock:
            for e in edits:
                key, iid, rev = str(e["agenda_id"]), e["item_id"], e["rev"]
                st = self.ag.setdefault(key, _new_state())
                item = next((it for it in st["items"] if it["id"] == iid), None)
                if e["action"] == "confirm":
                    if item is None or not item["edited"]:
                        ignored.append({"itemId": iid, "reason": "not_edited"})
                        continue
                    if rev <= self.item_rev.get(iid, rev - 1):
                        ignored.append({"itemId": iid, "reason": "stale"})
                        continue
                    item["needs_check"] = False
                    item["sig"] = None                       # 지금 글을 기준으로 다시 잡는다
                    item["ack"] = self._ack(item, key, view)
                else:
                    if rev <= self.item_rev.get(iid, rev - 1):
                        ignored.append({"itemId": iid, "reason": "stale"})
                        continue
                    if item is None:
                        item = {"id": iid}
                        st["items"].append(item)
                    item.update({"text": e["text"], "evidence": sorted(set(e["evidence_seqs"])), "edited": True,
                                 "needs_check": False, "sig": None})
                    item["ack"] = self._ack(item, key, view)
                self.item_rev[iid] = rev
                st["dirty"] = True                           # 다음 주기에 기다리지 않고 다시 요약
                applied += 1
        log.info("고친 요약 반영 meeting=%s edits=%s applied=%s", self.c.meeting_id, len(edits), applied)
        return {"applied": applied, "ignored": ignored}

    @staticmethod
    def _moved(item, key, view):
        """근거 줄 중 분류가 정해졌고 이 안건이 아닌 줄."""
        eff = view["eff"]
        return {s for s in item["evidence"] if s in eff and eff[s] != key}

    def _ack(self, item, key, view):
        """사용자가 이미 알고 있는 변화: 지금 옮겨 갔거나 사라진 근거 줄. 이 줄들 때문에 다시 확인 필요를 켜지 않는다."""
        return frozenset(self._moved(item, key, view) | {s for s in item["evidence"] if s in view["deleted"]})

    # ---------------- 요약 ----------------
    def process(self, final=False):
        """요약할 때가 된 안건을 요약한다. 반환: 모델을 부른 횟수."""
        view = self._view()
        with self.lock:
            if self.halted:
                return 0
            jobs = self._plan(view, final)
        calls = 0
        for job in jobs:
            calls += self._run_job(job)
            if self.halted:
                break
        return calls

    def _plan(self, view, final):
        """락 안에서 부른다. 상태를 정리하고, 모델을 불러야 할 안건의 일감을 만든다."""
        cfg = self.cfg
        keys, now = view["keys"], view["now_ms"]
        for k in [k for k in self.ag if k in self.known and k not in keys]:
            del self.ag[k]                                   # 목록에서 사라진 안건: 고친 문장까지 지운다 (계약 8장)
        self.known |= keys
        for a in view["agendas"]:
            self.ag.setdefault(a["key"], _new_state())

        lines_by_key, moved_in = {}, {}
        for ln in view["kept"]:
            s = ln["id"]
            k = view["eff"].get(s)
            if k is not None and k in keys:
                lines_by_key.setdefault(k, []).append(ln)
            prev = self.assign.get(s)
            if k is not None and k in keys and prev is not None and prev != k:
                moved_in.setdefault(k, set()).add(s)         # 다른 안건(또는 해당 없음)에서 옮겨 왔다
        for ln in view["kept"]:
            if ln["id"] in view["eff"]:
                self.assign[ln["id"]] = view["eff"][ln["id"]] or ""

        self._refresh_flags(view)

        jobs = []
        for a in view["agendas"]:
            key = a["key"]
            st = self.ag[key]
            lines = lines_by_key.get(key, [])
            cur = {ln["id"]: _h(ln["text"]) for ln in lines}
            refl = st["reflected"]
            lost = [i for i in refl if i not in cur]
            changed = [i for i in cur if i in refl and refl[i] != cur[i]]
            if lost or changed or moved_in.get(key):
                st["dirty"] = True                           # 줄이 옮겨 갔거나 글이 바뀜: 기다리지 않는다
            new_ids = [i for i in cur if i not in refl]
            trigger = False
            if st["dirty"]:
                trigger = True
            elif new_ids:
                if final or len(new_ids) >= cfg.min_new_lines:
                    trigger = True
                else:
                    if st["wait_since"] is None:
                        st["wait_since"] = now
                    trigger = now - st["wait_since"] >= cfg.refresh_sec * 1000
            else:
                st["wait_since"] = None
            if not trigger:
                continue
            locked = [it for it in st["items"] if it["edited"]]
            if not lines:                                    # 요약할 줄이 없다: 모델 없이 AI 문장만 비운다
                st["items"], st["reflected"], st["dirty"], st["wait_since"] = locked, {}, False, None
                continue
            lo, hi = window_range(min(len(lines), cfg.max_lines), cfg.max_sentences)
            need_hi = hi - len(locked)
            if need_hi <= 0:                                 # 고친 문장이 자리를 다 차지했다
                st["items"], st["reflected"], st["dirty"], st["wait_since"] = locked, cur, False, None
                continue
            jobs.append(self._make_job(a, st, lines, cur, locked, lo, need_hi))
            st["dirty"] = False
        return jobs

    def _make_job(self, agenda, st, lines, cur, locked, lo, need_hi):
        K = lines[-self.cfg.max_lines:]
        line_no = {ln["id"]: k for k, ln in enumerate(K, 1)}
        lo_eff = min(max(0 if locked else 1, lo - len(locked)), need_hi)
        parts = [f"다음은 하나의 안건에 연결된 회의 발언이야 (줄 {len(K)}개). 항목은 {lo_eff}~{need_hi}개 범위로 만들어줘.",
                 f"[안건]\n제목: {_clean(agenda['title'])}\n결정할 사항: {_clean(agenda['decision_point'])}"]
        prior = [it["text"] for it in st["items"] if not it["edited"]]
        if prior:
            parts.append("[이전 요약 - 앞뒤 맥락 이해용, 근거로 쓰거나 베끼지 마]\n" + "\n".join(f"- {t}" for t in prior))
        if locked:
            rows = []
            for it in locked:
                nums = sorted(line_no[s] for s in it["evidence"] if s in line_no)
                where = f"근거 줄 {nums[0]}~{nums[-1]}" if nums else "근거 줄이 이 전사에 없음"
                rows.append(f"- ({where}) {it['text']}")
            parts.append("[사람이 확정한 항목 - 이 내용은 다시 쓰지 마]\n" + "\n".join(rows))
        parts.append("[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {ln['text_marked']}" for k, ln in enumerate(K, 1)))
        return {"key": agenda["key"], "lines": K, "reflected": cur, "user": "\n\n".join(parts)}

    def _run_job(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur_user = job["user"], job["user"]
        results, calls = None, 0
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    log.warning("요약 호출 상한에 도달해 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
                    break
                self.calls += 1
            calls += 1
            try:
                raw = self.llm(cur_user)
            except Exception as e:                           # 키 오류, 네트워크 오류, 사용량 제한 등
                with self.lock:
                    self._fail(type(e).__name__)
                break
            try:
                items = parse_llm_json(raw)["summary"]
                if not isinstance(items, list):
                    raise ValueError("summary가 목록이 아님")
            except Exception as e:
                if attempt == 2:
                    with self.lock:
                        self._fail("ParseError")
                    break
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\nJSON 해석 실패({type(e).__name__}). 형식에 맞게 JSON만 다시 출력해."
                continue
            res = validate_items(items, job["lines"])
            bad = [(i, r) for i, r in enumerate(res, 1) if r[1]]
            if items and len(bad) == len(res) and attempt == 1:       # 쓸 수 있는 항목이 0개: 오류를 알려 주고 1회 더 요청
                msg = "\n".join(f"- 항목 {i}: " + " / ".join(r[1]) for i, r in bad)
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg}\n오류가 있는 항목을 고쳐서 전체 JSON을 다시 출력해."
                continue
            results = res
            break
        with self.lock:
            st = self.ag.get(job["key"])
            if results is None:                              # 실패: 반영하지 않고 다음 주기에 다시 한다
                if st is not None:
                    st["dirty"] = True
                return calls
            self.err_streak = 0
            if st is not None:
                self._commit(job, st, results)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        log.warning("요약 호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            log.error("요약을 멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    def _commit(self, job, st, results):
        K = job["lines"]
        locked = [it for it in st["items"] if it["edited"]]           # 호출하는 사이 사람이 고친 것까지 반영
        new_items, rejected = [], 0
        for it, errs, warns, spans in results:
            ids = sorted({K[k - 1]["id"] for a, b in spans for k in range(a, b + 1)}) if not errs else []
            if errs or not ids:
                rejected += 1
                continue
            self.serial += 1
            new_items.append({"id": f"{self.prefix}-{self.serial}", "text": it["text"], "evidence": ids,
                              "edited": False, "needs_check": False, "sig": None, "ack": frozenset(),
                              "warns": len(warns)})            # 검증기의 경고(근거에 없는 단어 후보 등). 거르지는 않고 개수만 센다
        old_ai = [it for it in st["items"] if not it["edited"]]
        if rejected and len(new_items) < len(old_ai):
            # 이번 결과가 검증에서 탈락해 이전보다 줄었으면, 근거 줄이 아직 있는 이전 문장을 지우지 않고 둔다
            alive = set(job["reflected"])
            keep = [it for it in old_ai if all(s in alive for s in it["evidence"])]
            if len(keep) > len(new_items):
                new_items = keep
                log.info("탈락으로 줄어 이전 문장 유지 meeting=%s kept=%s", self.c.meeting_id, len(keep))
        st["items"] = locked + new_items[: max(0, self.cfg.max_sentences - len(locked))]
        st["reflected"], st["wait_since"] = job["reflected"], None
        log.info("안건 요약 meeting=%s sentences=%s rejected=%s calls=%s", self.c.meeting_id, len(st["items"]), rejected, self.calls)

    # ---------------- 확인 필요 표시 ----------------
    def _refresh_flags(self, view):
        """고친 문장의 needsCheck 를 켠다 (계약 6장 6번). 한 번 켜지면 confirm 이나 다시 고칠 때까지 유지한다."""
        eff, by_id, deleted = view["eff"], view["by_id"], view["deleted"]
        for key, st in self.ag.items():
            for it in st["items"]:
                if not it["edited"] or it["needs_check"]:
                    continue
                ev = it["evidence"]
                if any(s in deleted and s not in it["ack"] for s in ev):
                    it["needs_check"] = True
                    continue
                if all(s in by_id for s in ev):               # 아직 오지 않은 줄이 있으면 비교하지 않는다 (재시작 직후)
                    sig = tuple((s, _h(by_id[s]["text"])) for s in ev)
                    if it["sig"] is None:
                        it["sig"] = sig
                    elif sig != it["sig"]:
                        it["needs_check"] = True
                        continue
                if self._moved(it, key, view) - it["ack"]:
                    it["needs_check"] = True

    # ---------------- 출력 ----------------
    def take_board(self):
        """안건 보드의 전체 상태. 안건 목록을 아직 못 받았거나 마지막으로 낸 것과 같으면 None.
        돌려준 내용은 보낸 것으로 기록한다."""
        view = self._view()
        with self.lock:
            if not view["agendas"]:
                return None
            self._refresh_flags(view)
            by_id = view["by_id"]
            agendas = []
            for a in view["agendas"]:
                st = self.ag.get(a["key"]) or _new_state()
                rows = [it for it in st["items"] if it["evidence"]]
                rows.sort(key=lambda it: min((by_id[s]["start_ms"] for s in it["evidence"] if s in by_id), default=float("inf")))
                agendas.append({"agendaId": view["ids"][a["key"]], "status": None,
                                "summary": [{"id": it["id"], "text": it["text"], "evidenceSeqs": list(it["evidence"]),
                                             "edited": bool(it["edited"]), "needsCheck": bool(it["needs_check"])} for it in rows],
                                "opinions": []})
            cur_id = view["ids"].get(view["current"]) if view["current"] is not None else None
            sig = _h(json.dumps([cur_id, agendas], ensure_ascii=False, sort_keys=True))
            if sig == self.sent_sig:
                return None
            self.sent_sig = sig
            self.version = max(int(self.now() * 1000), self.version + 1)
            based = max((s for st in self.ag.values() for s in st["reflected"]), default=0)
            return {"version": self.version, "basedOnSeq": based, "currentAgendaId": cur_id,
                    "agendas": agendas, "offTopic": None}

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 요약 문장은 넣지 않는다."""
        with self.lock:
            items = [it for st in self.ag.values() for it in st["items"]]
            return {"agendas": len(self.ag), "sentences": len(items), "edited": sum(1 for it in items if it["edited"]),
                    "needsCheck": sum(1 for it in items if it["needs_check"]),
                    "warned": sum(1 for it in items if it.get("warns")),
                    "calls": self.calls, "errors": self.errors, "halted": self.halted}


def _b36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


# --------------------------------------------------------------------------------------
def selftest():
    from agenda_classifier import AgendaClassifier, ClassifyCfg

    def ln(i, start, text, kind=None, low=False):
        return {"id": i, "start_ms": start, "end_ms": start + 1000, "arrival_ms": start + 1000, "type": "final",
                "kind": kind, "low_confidence": low, "text": text, "text_marked": text, "manual": False}

    def ag(i, title, decision, pos):
        return {"id": i, "key": str(i), "position": pos, "title": title, "decision_point": decision, "added": False}

    A1 = ag("a1", "배포 일정", "배포 날짜를 확정한다", 1)
    A2 = ag("a2", "예산 배분", "비용 한도를 정한다", 2)

    def fresh(agendas=(A1, A2), sum_kw=None, llm=None, **cls_kw):
        c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0, **cls_kw))
        if agendas:
            c.set_agendas(1, list(agendas))
        s = AgendaSummarizer(c, SummarizeCfg(**dict(refresh_sec=60, **(sum_kw or {}))), llm=llm, id_prefix="p")
        return c, s

    def feed(c, lines, sent_ms, edits=()):
        c.receive({"lines": lines, "edits": list(edits), "sent_ms": sent_ms})
        c.process()

    def board_of(b, aid):
        return next(a for a in b["agendas"] if a["agendaId"] == aid)

    # 로그에 전사·제목·문장이 남지 않는지 보려고 모든 로그를 모은다
    records = []

    class Grab(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    h = Grab()
    logging.getLogger("ai-service").addHandler(h)
    logging.getLogger("ai-service").setLevel(logging.DEBUG)

    # 1) 안건 목록이 없으면 보내지 않는다. 목록이 오면 빈 요약으로 한 번 낸다
    c, s = fresh(agendas=())
    assert s.take_board() is None
    c.set_agendas(1, [A1, A2])
    b = s.take_board()
    assert b["agendas"][0]["summary"] == [] and b["currentAgendaId"] is None and b["offTopic"] is None and b["basedOnSeq"] == 0, b
    assert [a["status"] for a in b["agendas"]] == [None, None] and all(a["opinions"] == [] for a in b["agendas"])
    assert s.take_board() is None, "같은 상태를 다시 냄"

    # 2) 새 줄 2개 이상이면 바로 요약. 그 안건만 부른다. 근거는 그 안건의 줄 번호
    c, s = fresh()
    feed(c, [ln(1, 0, "기밀단어 배포는 다음 주 목요일로 하죠"), ln(2, 4000, "배포 날짜는 목요일이 좋아요"),
             ln(3, 8000, "비용 한도는 얼마로 할까요")], 9000)
    assert s.process() == 1, "새 줄이 2개인 안건 하나만 요약해야 함"
    b = s.take_board()
    s1, s2 = board_of(b, "a1")["summary"], board_of(b, "a2")["summary"]
    assert s1 and set(sum((x["evidenceSeqs"] for x in s1), [])) <= {1, 2}, s1
    assert s2 == [], "줄이 1개뿐인 안건은 아직 기다림"
    assert all(x["edited"] is False and x["needsCheck"] is False for x in s1)
    assert len({x["id"] for x in s1}) == len(s1) and all(x["id"].startswith("p-") for x in s1)
    assert b["currentAgendaId"] == "a2" and b["basedOnSeq"] == 2 and b["version"] >= int(time.time() * 1000) - 5000, b
    assert len(s1) <= 3
    assert s.take_board() is None
    ai_id = s1[0]["id"]                                     # 고치지 않은(AI가 만든) 문장에 대한 confirm 은 무시
    assert s.apply_edits([{"agenda_id": "a1", "item_id": ai_id, "action": "confirm", "rev": 1}]) == \
        {"applied": 0, "ignored": [{"itemId": ai_id, "reason": "not_edited"}]}
    assert s.take_board() is None and not s.ag["a1"]["dirty"]

    # 3) 1줄은 refresh_sec(음성 시간) 동안 기다렸다가 요약한다. 그 전에는 모델을 부르지 않는다
    assert s.process() == 0
    feed(c, [ln(4, 20000, "배포 서버를 정해야 해요")], 21000)
    assert s.process() == 0, "새 줄 1개: 기다림"
    c.advance(70000)
    assert s.process() == 1, "60초가 지나면 요약"
    assert s.take_board() is not None
    n_calls = s.calls
    assert s.process() == 0 and s.calls == n_calls
    feed(c, [ln(5, 24000, "배포 서버 확인 필요"), ln(6, 28000, "배포 점검 일정"), ln(7, 32000, "배포 후 공유")], 33000)
    assert s.process() == 1, "새 줄 3개: 바로 요약"
    assert max(sum((x["evidenceSeqs"] for x in board_of(s.take_board(), "a1")["summary"]), [])) >= 5

    # 4) 줄이 다른 안건으로 옮겨 가면 기다리지 않고 두 안건을 모두 원문 줄에서 다시 요약한다
    c.apply_corrections([{"seq": 1, "agenda_id": "a2", "rev": 1}])
    assert s.process() == 2, "옮겨 간 안건과 받은 안건 모두 다시 요약"
    b = s.take_board()
    assert 1 not in sum((x["evidenceSeqs"] for x in board_of(b, "a1")["summary"]), []), "옮겨 간 줄이 이전 안건 근거에 남음"
    assert board_of(b, "a2")["summary"], b

    # 5) 사람이 고친 요약: 새 고정 문장, 낡은 rev·고치지 않은 문장의 confirm 은 무시
    r = s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "edit", "text": "배포일은 목요일로 정함",
                        "evidence_seqs": [2, 4], "rev": 5}])
    assert r == {"applied": 1, "ignored": []}, r
    r = s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "edit", "text": "다른 글", "evidence_seqs": [2], "rev": 5},
                       {"agenda_id": "a1", "item_id": "nope", "action": "confirm", "rev": 1}])
    assert r == {"applied": 0, "ignored": [{"itemId": "old-9", "reason": "stale"}, {"itemId": "nope", "reason": "not_edited"}]}, r
    assert s.process() == 1, "고친 안건은 기다리지 않고 다시 요약"
    b = s.take_board()
    its = board_of(b, "a1")["summary"]
    mine = [x for x in its if x["id"] == "old-9"]
    assert mine == [{"id": "old-9", "text": "배포일은 목요일로 정함", "evidenceSeqs": [2, 4], "edited": True, "needsCheck": False}], its
    ai = [x for x in its if not x["edited"]]
    assert len(its) <= 3 and len(ai) == 1 and all(x["id"].startswith("p-") for x in ai), its

    # 6) needsCheck (가) 근거 줄의 글이 바뀌면 켜진다. confirm 으로 꺼지고 다시 켜지지 않는다
    feed(c, [], 80000, edits=[{"id": 4, "rev": 1, "text": "배포 서버는 나중에 정해요", "text_marked": "배포 서버는 나중에 정해요"}])
    b = s.take_board()
    assert [x["needsCheck"] for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"] == [True], b
    r = s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "confirm", "rev": 6}])
    assert r["applied"] == 1
    assert s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "confirm", "rev": 6}])["ignored"][0]["reason"] == "stale"
    s.process()
    b = s.take_board()
    assert [x["needsCheck"] for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"] == [False], "confirm 뒤 다시 켜짐"

    # 7) needsCheck (나) 근거 줄이 다른 안건으로 옮겨 가면 켜진다. 문장은 그대로 두고, 확인한 뒤에는 다시 켜지지 않는다
    c.apply_corrections([{"seq": 2, "agenda_id": "a2", "rev": 1}])
    s.process()
    b = s.take_board()
    m = [x for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"][0]
    assert m["needsCheck"] is True and m["text"] == "배포일은 목요일로 정함" and m["edited"] is True, m
    s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "confirm", "rev": 7}])
    s.process()
    b = s.take_board()
    assert [x["needsCheck"] for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"] == [False]

    # 8) 근거 줄이 사라져도 켜진다. 확인하면 꺼지고, 사라진 줄 때문에 다시 켜지지 않는다
    feed(c, [], 85000, edits=[{"id": 4, "rev": 2, "deleted": True}])
    s.process()
    b = s.take_board()
    assert [x["needsCheck"] for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"] == [True]
    s.apply_edits([{"agenda_id": "a1", "item_id": "old-9", "action": "confirm", "rev": 8}])
    s.process()
    b = s.take_board()
    assert [x["needsCheck"] for x in board_of(b, "a1")["summary"] if x["id"] == "old-9"] == [False]

    # 9) 안건이 삭제되면 그 안건의 요약 상태(고친 문장 포함)를 지운다. 목록에 없던 안건의 고침은 보관했다가 목록이 오면 쓴다
    s.apply_edits([{"agenda_id": "zz", "item_id": "z-1", "action": "edit", "text": "늦게 온 안건의 문장", "evidence_seqs": [3], "rev": 1}])
    assert "zz" in s.ag
    s.process()
    assert "zz" in s.ag, "목록에 없던 안건의 고침을 버림"
    c.set_agendas(2, [A2, ag("zz", "새 안건", "아무거나", 2)])
    s.process()
    b = s.take_board()
    assert [a["agendaId"] for a in b["agendas"]] == ["a2", "zz"] and "a1" not in s.ag, b
    assert board_of(b, "zz")["summary"][0]["id"] == "z-1"
    c.set_agendas(3, [A2])
    s.process()
    assert "zz" not in s.ag and [a["agendaId"] for a in s.take_board()["agendas"]] == ["a2"]

    # 10) 호출 실패: 반영하지 않고 분류는 계속된다. 연속 3번이면 멈추고, 성공하면 연속 횟수가 0으로 돌아간다
    state = {"fail": True}

    def flaky(user):
        if state["fail"]:
            raise RuntimeError("boom")
        return echo_llm(user)
    c, s = fresh(llm=flaky)
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    assert s.process() == 1 and s.info()["errors"] == 1 and s.take_board()["agendas"][0]["summary"] == []
    state["fail"] = False
    assert s.process() == 1 and s.err_streak == 0 and board_of(s.take_board(), "a1")["summary"], "실패 뒤 다시 시도해야 함"
    state["fail"] = True
    c.apply_corrections([{"seq": 1, "agenda_id": "a2", "rev": 1}])
    for _ in range(3):
        s.process()
        c.apply_corrections([{"seq": 1, "agenda_id": "a1", "rev": 10 + _}])
        s.process()
    assert s.halted == "연속 실패" and s.process() == 0
    feed(c, [ln(9, 9000, "배포 점검 일정"), ln(10, 9500, "배포 점검 날짜")], 10000)   # 요약이 멈춰도 분류는 계속된다
    assert c.labels.get(9) == "a1"

    # 11) 호출 상한
    c, s = fresh(sum_kw={"max_calls": 1})
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일"), ln(3, 5000, "비용 한도 정하죠"), ln(4, 6000, "비용 한도는 얼마")], 7000)
    assert s.process() == 1 and s.halted == "호출 상한" and s.info()["calls"] == 1

    # 12) 검증에서 탈락해 결과가 줄면 이전 문장을 지우지 않는다
    good = {"on": True}

    def partial(user):
        if good["on"]:
            return echo_llm(user)
        return json.dumps({"summary": [{"text": "근거 없는 문장", "topic": None, "evidence": []}]}, ensure_ascii=False)
    c, s = fresh(llm=partial)
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일"), ln(3, 5000, "배포 서버 확인"), ln(4, 6000, "배포 점검")], 7000)
    s.process()
    before = board_of(s.take_board(), "a1")["summary"]
    assert before
    good["on"] = False
    feed(c, [ln(5, 8000, "배포 후 확인"), ln(6, 9000, "배포 결과 공유")], 10000)
    s.process()
    after = board_of(s.take_board() or {"agendas": [{"agendaId": "a1", "summary": before}]}, "a1")["summary"]
    assert [x["text"] for x in after] == [x["text"] for x in before], "탈락으로 줄었는데 이전 문장을 지움"

    # 12-2) 검증기: 전사에 없는 숫자가 든 문장은 통과하지 못하고, 한 번 더 요청한 뒤에도 같으면 비워 둔다
    def fabricate(user):
        return json.dumps({"summary": [{"text": "예산을 10000원으로 정함", "topic": None, "evidence": [[1, 1]]}]}, ensure_ascii=False)
    c, s = fresh(llm=fabricate)
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    assert s.process() == 2, "쓸 수 있는 항목이 0개면 오류를 알려 주고 1회 더 요청"
    assert board_of(s.take_board(), "a1")["summary"] == [], "전사에 없는 숫자가 든 문장이 통과함"

    # 12-3) 모델이 고른 근거 줄만 evidenceSeqs 가 된다 (안건의 모든 줄이 아니라)
    def cite_first(user):
        return json.dumps({"summary": [{"text": "배포 목요일 이야기가 나옴", "topic": None, "evidence": [[1, 1]]}]}, ensure_ascii=False)
    c, s = fresh(llm=cite_first)
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    s.process()
    assert [x["evidenceSeqs"] for x in board_of(s.take_board(), "a1")["summary"]] == [[1]]

    # 12-4) version 은 시계가 멈춰 있어도 계속 커진다
    c, s = fresh()
    s.now = lambda: 1000.0
    v1 = s.take_board()["version"]
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    s.process()
    v2 = s.take_board()["version"]
    assert v1 == 1000000 and v2 == 1000001, (v1, v2)

    # 13) 재시작 복구: 새 요약기에 안건 목록 -> 고친 문장 -> 줄 순서로 와도 고친 문장이 유지되고,
    #     같은 안건으로 분류되면 확인 필요가 켜지지 않는다. 다른 안건으로 분류되면 켜진다
    for target, expect in (("a1", False), ("a2", True)):
        c, s = fresh()
        s.apply_edits([{"agenda_id": "a1", "item_id": "old-1", "action": "edit", "text": "사람이 쓴 문장", "evidence_seqs": [1, 2], "rev": 3}])
        b = s.take_board()
        assert board_of(b, "a1")["summary"][0]["needsCheck"] is False, "줄이 오기 전에는 비교하지 않음"
        if target == "a2":
            c.apply_corrections([{"seq": 1, "agenda_id": "a2", "rev": 1}])
        feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
        s.process()
        b = s.take_board()
        mine = [x for x in board_of(b, "a1")["summary"] if x["id"] == "old-1"]
        assert mine and mine[0]["text"] == "사람이 쓴 문장" and mine[0]["needsCheck"] is expect, (target, mine)

    # 14) 회의가 끝나면(final) 기다리던 1줄도 요약한다
    c, s = fresh()
    feed(c, [ln(1, 0, "배포는 목요일로")], 1000)
    assert s.process() == 0 and s.process(final=True) == 1 and board_of(s.take_board(), "a1")["summary"]

    # 15) 고친 문장이 3개를 다 차지하면 모델을 부르지 않는다
    c, s = fresh()
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    s.apply_edits([{"agenda_id": "a1", "item_id": f"k-{i}", "action": "edit", "text": f"문장 {i}", "evidence_seqs": [1], "rev": 1} for i in range(3)])
    assert s.process() == 0
    assert [x["id"] for x in board_of(s.take_board(), "a1")["summary"]] == ["k-0", "k-1", "k-2"]

    # 16) 숫자·문자열 안건 번호: 받은 타입 그대로 돌려준다
    c = AgendaClassifier("t", ClassifyCfg(refresh_sec=0))
    c.set_agendas(1, [{"id": 101, "key": "101", "position": 1, "title": "배포 일정", "decision_point": "배포 날짜", "added": False}])
    s = AgendaSummarizer(c, SummarizeCfg(), id_prefix="p")
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    s.process()
    b = s.take_board()
    assert b["agendas"][0]["agendaId"] == 101 and b["currentAgendaId"] == 101, b
    json.dumps(b)

    # 17) 프롬프트: 시스템 프롬프트에 추가 규칙이 들어갔고, 사람이 확정한 항목은 "다시 쓰지 마"로, 이전 요약은 맥락으로만 준다
    seen = []

    def spy(user):
        seen.append(user)
        return echo_llm(user)
    c, s = fresh(llm=spy)
    feed(c, [ln(1, 0, "배포는 목요일로"), ln(2, 4000, "배포 날짜는 목요일")], 5000)
    s.process()
    c.receive({"lines": [ln(3, 8000, "배포 서버 확인"), ln(4, 9000, "배포 점검")], "edits": [], "sent_ms": 10000})
    c.process()
    s.apply_edits([{"agenda_id": "a1", "item_id": "h-1", "action": "edit", "text": "사람이 쓴 문장", "evidence_seqs": [1], "rev": 1}])
    s.process()
    assert "18. 지금 주는 전사는" in AGENDA_SYSTEM_PROMPT and "[사람이 확정한 항목" in seen[-1] and "[이전 요약" in seen[-1]
    assert "- (근거 줄 1~1) 사람이 쓴 문장" in seen[-1] and "제목: 배포 일정" in seen[-1] and "항목은 " in seen[-1]
    assert seen[0].count("[이전 요약") == 0, "처음에는 이전 요약이 없다"

    # 18) 로그에는 전사, 안건 제목, 요약 문장이 남지 않는다
    logging.getLogger("ai-service").removeHandler(h)
    text = "\n".join(records)
    for secret in ("기밀단어", "배포 일정", "사람이 쓴 문장", "늦게 온 안건의 문장"):
        assert secret not in text, f"로그에 내용이 남음: {secret}"
    assert records, "로그가 하나도 없음 (검사가 의미 없음)"
    print("agenda_summarizer selftest OK")


if __name__ == "__main__":
    selftest()
