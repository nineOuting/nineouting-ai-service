#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
안건별 항목 추출의 공통 뼈대 (AI-05 의견과 할 일이 함께 쓴다)

의견(agenda_opinions.py)과 할 일(agenda_todos.py)은 모델 호출을 따로 하지만, 입력을 만들고, 갱신 시점을 정하고, 근거를 검증하고,
사람이 고친 항목을 잠그는 방식은 같다. 이 파일에 그 뼈대를 두고, 두 파일은 프롬프트와 항목 모양만 정한다.
형식은 docs/api/agenda-opinions-contract.md, 예시는 examples/api/.

동작 (안건별 요약 agenda_summarizer.py, 안건 상태 판정 agenda_status.py 와 같은 구조)
  - 입력은 분류 결과가 그 안건인 원문 줄이다. 맞장구는 바로 앞에 분류된 줄의 안건에 붙인다(앞 줄 끝에서 backchannel_gap_sec 이내이고
    그 사이에 해당 없음 줄이 없을 때). 간투사, 낮은 확신도 줄, 해당 없음 줄은 뺀다. 요약 문장은 쓰지 않는다 (요약 위에 쌓지 않는다).
  - 갱신 때마다 그 안건의 원문 줄에서 새로 만든다. 이전에 만든 항목은 모델에 보여 주지 않는다.
  - 모든 항목은 근거 줄이 필수다. 인용·숫자·STT 불확실 표시는 meeting_summary.validate_items 로 확인하고, 오류가 있으면 알려 주고 1회 더 부른다.
    그래도 통과하지 못한 항목은 내지 않는다 (이때는 연속 실패로 세지 않는다).
  - 사람이 고친 항목은 잠근다: 다시 만들지 않고, 검증기도 거치지 않는다. 모델에는 "이미 있는 항목"으로 보여 같은 내용을 다시 만들지 않게 한다.
    근거 줄의 글이 바뀌거나 사라졌을 때, 근거 줄이 다른 안건으로 옮겨 갔을 때만 needsCheck 를 켠다.
  - 갱신 시점: 안건 줄이 min_lines 개 이상일 때부터. 새 줄이 min_new_lines 개 이상이면 바로, 1개면 refresh_sec(음성 시간) 뒤.
    줄이 옮겨 갔거나 글이 바뀌었거나 사람이 항목을 고친 안건은 기다리지 않는다. 회의가 끝나면(final) 바뀐 안건을 모두 한 번 더 처리한다.
  - 호출이 연속 3번 실패하거나 호출 상한에 닿으면 이 기능만 멈춘다 (분류, 상태, 요약, 다른 항목 추출은 영향받지 않는다).

스레드: 일꾼 스레드(process, board_items)와 요청 스레드(apply_edits)가 함께 쓴다. 상태 변경은 락 안에서 하고 모델 호출은 락 밖에서 한다.
        분류기의 락은 읽을 때만 잠깐 잡고(_view), 이 객체의 락과 겹쳐 잡지 않는다.

로그 규칙: 전사 내용, 안건 제목, 항목 문장, 이름을 남기지 않는다. 개수와 예외 이름만 남긴다.
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

from meeting_summary import validate_items, call_llm, parse_llm_json, mmss  # noqa: E402
from agenda_classifier import _clean  # noqa: E402
from agenda_summarizer import _b36  # noqa: E402

MAX_STREAK = 3                        # 연속 실패가 이만큼이면 이 기능을 멈춘다


class ItemCfg:
    """설정의 공통 틀. 값은 모두 제안이며 평가 뒤에 다시 정한다. 기능마다 MIN_LINES, MAX_ITEMS 기본값만 바꾼다."""
    MIN_LINES = 1
    MIN_NEW_LINES = 2
    MAX_ITEMS = 20

    def __init__(self, **kw):
        self.provider = "echo"
        self.model = None
        self.refresh_sec = 60             # 새 줄이 1개뿐일 때 기다리는 시간 (음성 시간 기준)
        self.min_new_lines = self.MIN_NEW_LINES            # 새 줄이 이만큼이면 기다리지 않고 바로 처리
        self.min_lines = self.MIN_LINES   # 안건에 연결된 줄이 이만큼 이상일 때부터 호출
        self.max_items = self.MAX_ITEMS   # 안건당 항목 수 (사람이 고친 항목이 먼저 자리를 차지)
        self.max_calls = 300              # 회의당 모델 호출 상한 (비용을 추정한 뒤 정한다)
        self.max_lines = 200              # 한 번에 모델에 주는 줄 수 (넘으면 가장 최근 줄)
        self.backchannel_gap_sec = 10     # 맞장구를 앞 줄의 안건에 붙이는 최대 간격 (앞 줄의 끝부터, 음성 시간)
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def _h(s):
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:10]


def _new_state():
    return {"items": [], "reflected": {}, "wait_since": None, "dirty": False}


class AgendaItemExtractor:
    """하위 클래스가 정할 것: TASK, TARGET, LETTER, ROOT, SYSTEM_PROMPT, echo(user), _intro, _candidate, _check, _edit_data,
    _locked_text, _board_item, (선택) _sort_items."""
    TASK = None                       # 토큰·호출 시간 지표의 ai.task 값
    TARGET = None                     # 고친 항목 요청의 target 값 ("opinion" 또는 "todo")
    LETTER = "x"                      # 항목 번호에 들어가는 글자
    ROOT = None                       # 모델 JSON 의 최상위 키
    SYSTEM_PROMPT = ""

    def __init__(self, classifier, cfg, llm=None, id_prefix=None, now=time.time):
        self.c = classifier
        self.cfg = cfg
        self.llm = llm or self._make_llm()
        self.now = now
        self.log = logging.getLogger("ai-service." + self.TASK.replace("_", "-"))
        self.lock = threading.RLock()
        # 항목 번호: 재시작해도 겹치지 않도록 시작 시각과 무작위 값을 섞는다
        self.prefix = id_prefix or (_b36(int(now() * 1000)) + secrets.token_hex(1))
        self.serial = 0
        self.ag = {}                  # 안건 key -> 상태 {"items", "reflected", "wait_since", "dirty"}
        self.known = set()            # 안건 목록에 한 번이라도 있었던 key (사라졌는지 가리는 데 쓴다)
        self.assign = {}              # seq -> 지난번에 본 안건 key ("" 이면 해당 없음). 줄이 옮겨 갔는지 알아내는 데 쓴다
        self.item_rev = {}            # 항목 번호 -> 마지막으로 반영한 rev
        self.calls = 0
        self.errors = 0
        self.err_streak = 0
        self.rejected = 0             # 검증을 통과하지 못해 내지 않은 항목 수
        self.halted = None            # 멈춘 이유 (None이면 정상)

    def _make_llm(self):
        if self.cfg.provider == "echo":
            return self.echo
        return lambda user: call_llm(self.cfg.provider, self.cfg.model, self.SYSTEM_PROMPT, user, task=self.TASK)

    # ---------------- 하위 클래스가 정하는 것 ----------------
    def echo(self, user):                                   # pragma: no cover (하위 클래스가 정한다)
        raise NotImplementedError

    def _intro(self, n_lines, need):
        raise NotImplementedError

    def _candidate(self, d):
        """모델이 낸 항목 -> validate_items 가 보는 {"text", "topic", "evidence"}."""
        raise NotImplementedError

    def _check(self, d, pool):
        """검증기를 통과한 항목의 종류별 검사. 반환: (오류 목록, 저장할 data). pool 은 근거 줄들."""
        raise NotImplementedError

    def _edit_data(self, e):
        """어댑터를 거친 고침 -> 항목 data."""
        raise NotImplementedError

    def _locked_text(self, it):
        raise NotImplementedError

    def _board_item(self, it):
        raise NotImplementedError

    def _sort_items(self, items, by_id):
        return sorted(items, key=lambda it: not it["edited"])      # 사람이 고친 항목이 앞, 나머지는 모델이 낸 순서

    def _parse(self, raw):
        items = parse_llm_json(raw)[self.ROOT]
        if not isinstance(items, list):
            raise ValueError(f"{self.ROOT}가 목록이 아님")
        return items

    # ---------------- 분류기에서 읽기 ----------------
    def _view(self):
        """분류기의 지금 상태를 복사해 온다 (분류기 락은 여기서만 잠깐 잡는다).
        rows: 안건 key -> [(줄, 맞장구인가)] 시간 순서. 맞장구는 바로 앞에 분류된 줄의 안건에 붙인다."""
        c = self.c
        gap = self.cfg.backchannel_gap_sec * 1000
        with c.lock:
            snap = [dict(ln) for ln in c.store.snapshot()]
            keys = set(c.keys)
            classified = {ln["id"] for ln in snap if ln["id"] in c.locks or ln["id"] in c.labels}
            eff = {i: c._eff(i) for i in classified}
            agendas = [dict(a) for a in c.agendas]
            deleted = set(c.store.deleted)
            now_ms = c.now_ms
        rows, last_key, last_end = {}, None, None
        for ln in snap:
            if ln.get("low_confidence") or ln.get("kind") == "filler":
                continue
            if ln.get("kind") == "backchannel":
                if last_key is not None and ln["start_ms"] - last_end <= gap:
                    rows.setdefault(last_key, []).append((ln, True))
                continue
            i = ln["id"]
            if i not in classified:
                continue                                     # 아직 분류하지 못한 줄
            k = eff[i]
            if k is not None and k in keys:
                rows.setdefault(k, []).append((ln, False))
                last_key, last_end = k, (ln.get("end_ms") or ln["start_ms"])
            else:
                last_key = last_end = None                   # 해당 없음 줄이 끼면 그 뒤 맞장구는 붙이지 않는다
        return {"agendas": agendas, "keys": keys, "rows": rows, "eff": eff, "deleted": deleted, "now_ms": now_ms,
                "by_id": {ln["id"]: {"text": ln["text"], "start_ms": ln["start_ms"]} for ln in snap}}

    # ---------------- 사람이 고친 항목 ----------------
    def apply_edits(self, edits):
        """edits: 어댑터를 거친 고침 목록 (이 기능의 target 인 것만). 반환: {"applied": 새로 반영한 수, "ignored": [{"itemId", "reason"}]}
        reason 은 "stale" 또는 "not_edited"."""
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
                    item.update({"data": self._edit_data(e), "evidence": sorted(set(e["evidence_seqs"])), "edited": True,
                                 "needs_check": False, "sig": None})
                    item["ack"] = self._ack(item, key, view)
                self.item_rev[iid] = rev
                st["dirty"] = True                           # 다음 주기에 기다리지 않고 다시 처리
                applied += 1
        self.log.info("고친 항목 반영 meeting=%s edits=%s applied=%s", self.c.meeting_id, len(edits), applied)
        return {"applied": applied, "ignored": ignored}

    @staticmethod
    def _moved(item, key, view):
        """근거 줄 중 분류가 정해졌고 이 안건이 아닌 줄."""
        eff = view["eff"]
        return {s for s in item["evidence"] if s in eff and eff[s] != key}

    def _ack(self, item, key, view):
        """사용자가 이미 알고 있는 변화: 지금 옮겨 갔거나 사라진 근거 줄. 이 줄들 때문에 다시 확인 필요를 켜지 않는다."""
        return frozenset(self._moved(item, key, view) | {s for s in item["evidence"] if s in view["deleted"]})

    # ---------------- 처리 ----------------
    def process(self, final=False):
        """처리할 때가 된 안건을 처리한다. 반환: 모델을 부른 횟수."""
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
            del self.ag[k]                                   # 목록에서 사라진 안건: 고친 항목까지 지운다
        self.known |= keys
        for a in view["agendas"]:
            self.ag.setdefault(a["key"], _new_state())

        moved_in = {}
        for key, rows in view["rows"].items():
            for ln, bc in rows:
                if bc:
                    continue
                prev = self.assign.get(ln["id"])
                if prev is not None and prev != key:
                    moved_in.setdefault(key, set()).add(ln["id"])       # 다른 안건(또는 해당 없음)에서 옮겨 왔다
        for i, k in view["eff"].items():
            self.assign[i] = k if (k is not None and k in keys) else ""

        self._refresh_flags(view)

        jobs = []
        for a in view["agendas"]:
            key = a["key"]
            st = self.ag[key]
            rows = view["rows"].get(key, [])
            locked = [it for it in st["items"] if it["edited"]]
            n_lines = sum(1 for _ln, bc in rows if not bc)
            if n_lines < cfg.min_lines:                      # 줄이 적다: 모델 없이 AI 항목만 비운다
                if st["reflected"] or len(locked) != len(st["items"]):
                    st["items"], st["reflected"], st["dirty"], st["wait_since"] = locked, {}, False, None
                continue
            cur = {ln["id"]: _h(ln["text"]) for ln, _bc in rows}
            bc_ids = {ln["id"] for ln, bc in rows if bc}
            refl = st["reflected"]
            lost = [i for i in refl if i not in cur]
            changed = [i for i in cur if i in refl and refl[i] != cur[i]]
            if lost or changed or moved_in.get(key):
                st["dirty"] = True                           # 줄이 옮겨 갔거나 글이 바뀜: 기다리지 않는다
            new_ids = [i for i in cur if i not in refl and i not in bc_ids]    # 맞장구만 늘어난 것은 새 줄로 세지 않는다
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
            need = cfg.max_items - len(locked)
            if need <= 0:                                    # 고친 항목이 자리를 다 차지했다
                st["items"], st["reflected"], st["dirty"], st["wait_since"] = locked, cur, False, None
                continue
            jobs.append(self._make_job(a, rows, cur, locked, need))
            st["dirty"] = False
        return jobs

    def _make_job(self, agenda, rows, cur, locked, need):
        K = rows[-self.cfg.max_lines:]
        lines = [ln for ln, _bc in K]
        line_no = {ln["id"]: k for k, ln in enumerate(lines, 1)}
        parts = [self._intro(len(K), need),
                 f"[안건]\n제목: {_clean(agenda['title'])}\n결정할 사항: {_clean(agenda['decision_point'])}"]
        if locked:
            out = []
            for it in locked:
                nums = sorted(line_no[s] for s in it["evidence"] if s in line_no)
                where = f"근거 줄 {nums[0]}~{nums[-1]}" if nums else "근거 줄이 이 전사에 없음"
                out.append(f"- ({where}) {self._locked_text(it)}")
            parts.append("[사람이 확정한 항목 - 이 내용은 다시 쓰지 마]\n" + "\n".join(out))
        parts.append("[전사]\n" + "\n".join(f"[{k}] {mmss(ln['start_ms'])} {'[맞장구] ' if bc else ''}{ln['text_marked']}"
                                          for k, (ln, bc) in enumerate(K, 1)))
        return {"key": agenda["key"], "lines": lines, "reflected": cur, "user": "\n\n".join(parts)}

    def _validate(self, items, lines):
        """모델이 낸 항목을 검증한다. 반환: (통과한 항목 [{"data", "evidence", "warns"}], 오류 [(번호, [메시지])])."""
        cands = [self._candidate(d) for d in items]
        res = validate_items(cands, lines)
        good, errors = [], []
        for n, (d, (_it, errs, warns, spans)) in enumerate(zip(items, res), 1):
            if errs:
                errors.append((n, errs))
                continue
            pool = [lines[k - 1] for a, b in spans for k in range(a, b + 1)]
            cerrs, data = self._check(d if isinstance(d, dict) else {}, pool)
            if cerrs:
                errors.append((n, cerrs))
                continue
            ids = sorted({lines[k - 1]["id"] for a, b in spans for k in range(a, b + 1)})
            good.append({"data": data, "evidence": ids, "warns": len(warns)})
        return good, errors

    def _run_job(self, job):
        """모델을 부르고 결과를 반영한다. 모델 호출은 락 밖에서 한다. 반환: 부른 횟수."""
        user, cur_user = job["user"], job["user"]
        results, rejected, calls = None, 0, 0
        for attempt in (1, 2):
            with self.lock:
                if self.calls >= self.cfg.max_calls:
                    self.halted = "호출 상한"
                    self.log.warning("호출 상한에 도달해 멈춤 meeting=%s calls=%s", self.c.meeting_id, self.calls)
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
                items = self._parse(raw)
            except Exception as e:
                if attempt == 2:
                    with self.lock:
                        self._fail("ParseError")
                    break
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\nJSON 해석 실패({type(e).__name__}). 형식에 맞게 JSON만 다시 출력해."
                continue
            good, errors = self._validate(items, job["lines"])
            if errors and attempt == 1:                      # 오류가 있는 항목이 있으면 알려 주고 1회 더 요청
                msg = "\n".join(f"- 항목 {n}: " + " / ".join(es) for n, es in errors)
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg}\n오류가 있는 항목을 고쳐서 전체 JSON을 다시 출력해."
                continue
            results, rejected = good, len(errors)
            break
        with self.lock:
            st = self.ag.get(job["key"])
            if results is None:                              # 실패: 반영하지 않고 다음 주기에 다시 한다
                if st is not None:
                    st["dirty"] = True
                return calls
            self.err_streak = 0
            if st is not None:
                self._commit(job, st, results, rejected)
        return calls

    def _fail(self, name):
        self.errors += 1
        self.err_streak += 1
        self.log.warning("호출 실패 meeting=%s error=%s 연속=%s", self.c.meeting_id, name, self.err_streak)
        if self.err_streak >= MAX_STREAK:
            self.halted = "연속 실패"
            self.log.error("멈춤 meeting=%s 연속 실패 %s번", self.c.meeting_id, self.err_streak)

    def _commit(self, job, st, results, rejected):
        locked = [it for it in st["items"] if it["edited"]]           # 호출하는 사이 사람이 고친 것까지 반영
        new_items = []
        for g in results:
            self.serial += 1
            new_items.append({"id": f"{self.prefix}-{self.LETTER}{self.serial}", "data": g["data"], "evidence": g["evidence"],
                              "edited": False, "needs_check": False, "sig": None, "ack": frozenset(), "warns": g["warns"]})
        st["items"] = locked + new_items[: max(0, self.cfg.max_items - len(locked))]
        st["reflected"], st["wait_since"] = job["reflected"], None
        if rejected:
            self.rejected += rejected
        self.log.info("항목 갱신 meeting=%s items=%s rejected=%s calls=%s", self.c.meeting_id, len(st["items"]), rejected, self.calls)

    # ---------------- 확인 필요 표시 ----------------
    def _refresh_flags(self, view):
        """고친 항목의 needsCheck 를 켠다. 한 번 켜지면 confirm 이나 다시 고칠 때까지 유지한다."""
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
    def board_items(self):
        """안건 key -> 안건 보드에 담을 항목 목록 (모든 안건, 없으면 빈 목록)."""
        view = self._view()
        with self.lock:
            self._refresh_flags(view)
            out = {}
            for a in view["agendas"]:
                st = self.ag.get(a["key"]) or _new_state()
                rows = self._sort_items([it for it in st["items"] if it["evidence"]], view["by_id"])
                out[a["key"]] = [self._board_item(it) for it in rows]
            return out

    def based_on_seq(self):
        with self.lock:
            return max((s for st in self.ag.values() for s in st["reflected"]), default=0)

    def info(self):
        """상태 조회용. 전사 내용, 안건 제목, 항목 문장은 넣지 않는다."""
        with self.lock:
            items = [it for st in self.ag.values() for it in st["items"]]
            return {"agendas": len(self.ag), "items": len(items), "edited": sum(1 for it in items if it["edited"]),
                    "needsCheck": sum(1 for it in items if it["needs_check"]),
                    "calls": self.calls, "errors": self.errors, "rejected": self.rejected, "halted": self.halted}
