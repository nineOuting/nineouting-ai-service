#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
회의 중 주기 요약.

끝난 회의 요약(meeting_summary.py)과 같은 폴더에 두고 쓴다 (전사 저장소, 입력 정리, 검증기, 모델 호출을 가져다 쓴다).

흐름
  1) 전사를 말이 끝난 시각(end_ms) 기준으로 고정 길이 창(기본 120초)으로 나눈다.
  2) 갱신 주기(기본 60초)마다, 지난 창은 한 번 확정하고 아직 진행 중인 마지막 창(꼬리)만 다시 요약한다.
     확정된 창은 STT가 앞의 줄을 고치거나 지울 때(edits)만 그 창 하나를 다시 요약한다.
  3) 요약 위에 요약을 쌓지 않는다. 항상 그 창의 원문 줄에서 새로 만들고, 앞 맥락은 이전 요약 문장만 참고용으로 준다.
  4) 항목 id, 잠금(사람이 고치거나 확정함), 확인 필요 표시는 summary[] 와 따로 live_state.json 에 둔다.
     summary[] 는 저장 형식 v0.1 그대로 내보낸다.
  5) 정해 둔 시점(기본 10분, 20분, 종료)의 요약 상태를 따로 저장해, 그 시점까지의 글로만 답하는 QA 시험을 만든다.

비용이 드는 건 --provider gemini 처럼 실제 모델을 고를 때뿐이다.
기본값 echo 는 모델을 부르지 않는 모의 요약기라서 비용이 없다.
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import (  # noqa: E402
    TranscriptStore, load_jsonl_as_protocol_lines, make_batches, build_input,
    SYSTEM_PROMPT, call_llm, parse_llm_json, validate_items, build_char_stream, unknown_terms,
    spans_to_evidence, mmss, fmt_t, GENERIC_END, DECISION_END, MAX_ITEM_LINES, MAX_SPAN_LINES, load_dotenv,
)

GRACE_MS = 3000          # recovered 줄은 말이 끝나고 약 2초 뒤 도착한다 (규격). 여유 1초를 더한 값
WINDOW_GAP_LINES = 5     # 한 창 안에서 어느 항목에도 안 들어간 줄이 이만큼 연속이면 경고
USD_KRW_ASSUMED = 1400   # 비용 추정용 환율 가정 (실제와 다를 수 있음)
PRICES = {               # 100만 토큰당 (입력, 출력) USD. summary-eval-results-v1.md 7장의 공식 가격표 기준
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.0, 12.0),
}

LIVE_ADDENDUM = """
18. 지금 주는 전사는 회의 전체가 아니라 한 구간이야. 근거 줄 번호는 이 구간의 번호만 써.
19. [이전 구간 요약]은 "그 역할", "아까 말한 것" 같은 말이 무엇을 가리키는지 이해하는 데만 써. 거기 있는 문장을 베끼거나 근거로 쓰지 마. 이 구간 줄에 없는 내용은 쓰지 마.
20. 구간이 아직 진행 중이면 마지막 줄에서 말이 끊겼을 수 있어. 끊긴 말을 이어서 짐작하지 마.
21. [사람이 확정한 항목]이 있으면 그 항목이 다룬 내용은 다시 쓰지 말고, 나머지 내용만 항목으로 만들어.
22. 이 구간에 요약할 내용이 없으면 summary를 빈 목록으로 줘.
23. 전사의 ⟨ ⟩ 기호는 STT 불확실 글자를 표시한 것일 뿐이야. 「」 인용 안에는 ⟨ ⟩를 넣지 말고, 기호만 빼고 글자를 원문 그대로 옮겨.
24. STT 불확실 글자가 든 말을 「」로 인용하면 본문에 "STT 불확실"이라고 꼭 적어. 숫자는 뒤에 이어지는 글자까지 빼지 말고 끝까지 인용해.
25. 전사에 없는 명사나 해석(예: "제품", "유튜브")을 덧붙이지 마. 말한 내용만 옮겨."""

LIVE_SYSTEM_PROMPT = SYSTEM_PROMPT.replace("\n\n출력은 JSON만.", LIVE_ADDENDUM + "\n\n출력은 JSON만.")
assert LIVE_SYSTEM_PROMPT != SYSTEM_PROMPT, "시스템 프롬프트에 추가 규칙을 끼워 넣지 못함 (meeting_summary.py 버전 확인)"


# --------------------------------------------------------------------------------------
# 0. 설정과 모의 요약기
# --------------------------------------------------------------------------------------
class Cfg:
    """기본값을 가진 설정 묶음 (argparse 없이도 쓸 수 있게)."""

    def __init__(self, **kw):
        self.window_sec = 120
        self.refresh_sec = 60
        self.max_items = 3            # PRD AI-03: 안건(구간)별 2~3줄
        self.min_open_lines = 4       # 진행 중인 창은 요약할 줄이 이만큼 모이면 시작
        self.retries = 0              # 검증 실패 때 다시 요청하는 횟수 (호출이 그만큼 늘어남)
        self.max_calls = 60           # 모델 호출 상한 (넘으면 멈춤)
        self.context_windows = 2      # 맥락용으로 주는 이전 구간 수
        self.provider = "echo"
        self.model = None
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"알 수 없는 설정: {k}")
            setattr(self, k, v)


def window_range(n_lines, max_items):
    """창 하나에서 만들 항목 수 범위 (최소, 최대)."""
    if n_lines <= 0:
        return 0, 0
    lo = 1 if n_lines < 8 else min(2, max_items)
    hi = max(lo, min(max_items, -(-n_lines // 3)))
    return lo, hi


def echo_llm(user):
    """모델을 부르지 않는 모의 요약기. 구조(번호, 근거 구간, 개수)만 맞춰 항목을 만든다. 비용 0."""
    m = re.search(r"항목은 (\d+)~(\d+)개", user)
    lo, hi = (int(m.group(1)), int(m.group(2))) if m else (1, 2)
    body = user.split("[전사]\n", 1)[-1]
    rows = re.findall(r"^\[(\d+)\] (\d\d:\d\d) (.*)$", body, re.M)
    n = len(rows)
    if n == 0 or hi <= 0:
        return json.dumps({"summary": []}, ensure_ascii=False)
    k = min(n, hi, max(lo, 2))
    items = []
    for i in range(k):
        a, b = n * i // k, n * (i + 1) // k - 1
        first = re.sub(r"[⟨⟩]", "", rows[a][2])[:20]
        items.append({"text": f"{first} 등의 말이 나옴", "topic": None,
                      "evidence": [[int(rows[a][0]), int(rows[b][0])]]})
    return json.dumps({"summary": items}, ensure_ascii=False)


def make_llm(cfg):
    if cfg.provider == "echo":
        return echo_llm
    return lambda user: call_llm(cfg.provider, cfg.model, LIVE_SYSTEM_PROMPT, user, task="live_summary")


def _sha(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------------------
# 1. 주기 요약기
# --------------------------------------------------------------------------------------
class LiveSummarizer:
    def __init__(self, meeting_id, cfg, llm=None, date=None, out_dir=None, checkpoints_sec=(), verbose=True):
        self.meeting_id = meeting_id
        self.cfg = cfg
        self.llm = llm or make_llm(cfg)
        self.date = date or datetime.date.today().isoformat()
        self.out_dir = out_dir
        self.verbose = verbose
        self.W = cfg.window_sec * 1000
        self.R = cfg.refresh_sec * 1000
        self.store = TranscriptStore(meeting_id)
        self.windows = {}
        self.now_ms = 0
        self.next_tick_ms = self.R + GRACE_MS
        self.calls = 0
        self.halted = False
        self.err_streak = 0
        self.call_log = []
        self.events = []
        self.prompt_chars = 0          # --plan 용: 지금까지 보낸 글자 수
        self.checkpoints_ms = sorted(int(c) * 1000 for c in checkpoints_sec)
        self.snapshots = {}            # label -> {"label","now_ms","summary_doc"}
        self.prev = (0, self._empty_doc())
        self.last_tick_ms = 0

    # ---------------- 입력 ----------------
    def receive(self, batch):
        """STT 전송 묶음 하나를 받는다. 갱신 시각이 지났으면 한 번 갱신한다."""
        self.store.apply_batch(batch)
        if "sent_ms" in batch:
            self.now_ms = max(self.now_ms, batch["sent_ms"])
        if self.now_ms >= self.next_tick_ms:
            self._tick(self.now_ms, final=False)
            while self.next_tick_ms <= self.now_ms:
                self.next_tick_ms += self.R

    def finish(self):
        """회의 종료 신호. 남은 창을 모두 마지막으로 요약하고 닫는다."""
        self._tick(self.now_ms, final=True)
        self.snapshots["end"] = {"label": "end", "now_ms": self.now_ms, "summary_doc": self.export_summary(),
                                 "halted": self.halted}

    # ---------------- 사람이 하는 일 (UI가 부를 함수) ----------------
    def find_item(self, item_id):
        for w in self.windows.values():
            for it in w["items"]:
                if it["id"] == item_id:
                    return w, it
        raise KeyError(item_id)

    def lock_item(self, item_id, new_text=None):
        """사람이 문장을 고치거나(new_text) 확정하면 잠근다. 이후 갱신에서 AI가 덮어쓰지 않는다."""
        w, it = self.find_item(item_id)
        by_id = {l["id"]: l for l in self.store.snapshot()}
        if new_text is not None and new_text != it["text"]:
            it["text"] = new_text
            it["edited"] = True
        it["locked"] = True
        it["needs_review"] = False
        it["lock_sig"] = self._ids_sig(it["line_ids"], by_id)
        self._log(f"잠금: {item_id}" + (" (문장 수정)" if it["edited"] else ""))

    def unlock_item(self, item_id):
        w, it = self.find_item(item_id)
        it["locked"] = False
        it["needs_review"] = False
        it["lock_sig"] = None
        self._log(f"잠금 해제: {item_id}")

    def confirm_review(self, item_id):
        """'확인 필요'를 사람이 확인했다고 표시하고 기준을 현재 전사로 다시 잡는다."""
        self.lock_item(item_id)

    # ---------------- 내보내기 ----------------
    def _empty_doc(self):
        return {"schema_version": "0.1", "meeting_id": self.meeting_id, "date": getattr(self, "date", ""),
                "summary": [], "decisions": [], "action_items": [], "extensions": None}

    def export_summary(self):
        """저장 형식 v0.1 그대로. id, 잠금 같은 칸은 넣지 않는다 (live_state.json 에 따로 있음)."""
        items = []
        for idx in sorted(self.windows):
            for it in self.windows[idx]["items"]:
                items.append((idx, it["evidence"][0]["start"] if it["evidence"] else 0.0, it))
        items.sort(key=lambda x: (x[0], x[1]))
        doc = self._empty_doc()
        doc["summary"] = [{"text": it["text"], "topic": it.get("topic"), "evidence": it["evidence"]} for _, _, it in items]
        return json.loads(json.dumps(doc, ensure_ascii=False))

    def state(self):
        wins = [self.windows[i] for i in sorted(self.windows)]
        return {
            "schema": "live-0.1",
            "meeting_id": self.meeting_id,
            "config": {k: v for k, v in vars(self.cfg).items()},
            "now_ms": self.now_ms,
            "calls": self.calls,
            "halted": self.halted,
            "windows": wins,
            "call_log": self.call_log,
            "events": self.events,
        }

    # ---------------- 내부 ----------------
    def _log(self, msg):
        self.events.append({"now_ms": self.now_ms, "msg": msg})
        if self.verbose:
            print(f"[{mmss(self.now_ms)}] {msg}")

    @staticmethod
    def _sig(kept):
        return _sha("|".join(f"{l['id']}:{l['text_marked']}" for l in kept))

    @staticmethod
    def _ids_sig(ids, by_id):
        return _sha("|".join(f"{i}:{by_id[i]['text_marked']}" if i in by_id else f"{i}:DEL" for i in ids))

    def _new_window(self, idx):
        w = {"idx": idx, "start_ms": idx * self.W, "end_ms": (idx + 1) * self.W, "status": "open",
             "signature": None, "summarized_at_ms": None, "kept_lines": 0, "dropped_lines": 0,
             "calls": 0, "serial": 0, "rejected_items": 0, "notes": [], "items": []}
        self.windows[idx] = w
        return w

    def _save_due_snapshots(self, now):
        for c in self.checkpoints_ms:
            label = f"{c // 60000}min"
            if label not in self.snapshots and c < now:
                self.snapshots[label] = {"label": label, "now_ms": self.prev[0], "summary_doc": self.prev[1]}

    def _tick(self, now, final):
        if self.halted:
            return
        self._save_due_snapshots(now)
        snap = self.store.snapshot()
        by_id = {l["id"]: l for l in snap}
        all_kept, _d, _t = build_input(snap)
        full_stream, _m = build_char_stream(all_kept)
        groups = {}
        for l in snap:
            groups.setdefault(l["end_ms"] // self.W, []).append(l)
        for idx in sorted(set(groups) | set(self.windows)):
            w = self.windows.get(idx) or self._new_window(idx)
            kept, dropped, text = build_input(groups.get(idx, []))
            sig = self._sig(kept)
            sealable = final or (w["end_ms"] + GRACE_MS <= now)
            if w["status"] == "sealed" and sig == w["signature"]:
                continue
            if sig != w["signature"]:
                if not kept:
                    w["items"] = [it for it in w["items"] if it["locked"]]
                    w["signature"] = sig
                    w["kept_lines"], w["dropped_lines"] = 0, len(dropped)
                elif (not sealable) and len(kept) < self.cfg.min_open_lines:
                    pass   # 아직 내용이 적어 기다림
                else:
                    self._summarize(w, kept, dropped, text, full_stream, now, sig, is_open=not sealable)
                    if self.halted:
                        self._autosave()
                        return
            if sealable and w["status"] != "sealed" and w["signature"] == sig:
                w["status"] = "sealed"      # 요약에 실패한 창은 닫지 않고 다음 갱신 때 다시 시도
                self._log(f"창 {idx} ({mmss(w['start_ms'])}–{mmss(w['end_ms'])}) 확정, 항목 {len(w['items'])}개")
        self._refresh_review_flags(by_id)
        self.last_tick_ms = now
        self.prev = (now, self.export_summary())
        self._autosave()

    def _autosave(self):
        """갱신할 때마다 상태를 저장해, 중간에 멈춰도 지금까지의 결과가 남게 한다."""
        if not self.out_dir:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        with open(os.path.join(self.out_dir, "live_state.json"), "w", encoding="utf-8") as f:
            json.dump(self.state(), f, ensure_ascii=False, indent=2)

    def _refresh_review_flags(self, by_id):
        for w in self.windows.values():
            for it in w["items"]:
                if it["locked"] and it["lock_sig"] is not None and not it["needs_review"]:
                    if self._ids_sig(it["line_ids"], by_id) != it["lock_sig"]:
                        it["needs_review"] = True
                        self._log(f"확인 필요: {it['id']} (잠근 뒤 근거 줄이 바뀌거나 사라짐)")

    def _prior_texts(self, idx):
        out = []
        for j in range(idx - 1, -1, -1):
            if len(out) >= self.cfg.context_windows * self.cfg.max_items:
                break
            w = self.windows.get(j)
            if w and w["items"]:
                out = [it["text"] for it in w["items"]] + out
        return out[-self.cfg.context_windows * self.cfg.max_items:]

    def _summarize(self, w, kept, dropped, text, full_stream, now, sig, is_open):
        idx = w["idx"]
        locked = [it for it in w["items"] if it["locked"]]
        n = len(kept)
        lo, hi = window_range(n, self.cfg.max_items)
        need_hi = hi - len(locked)
        w["kept_lines"], w["dropped_lines"] = n, len(dropped)
        if need_hi <= 0:
            w["items"] = locked
            w["signature"], w["summarized_at_ms"] = sig, now
            w["notes"].append(f"{mmss(now)} 잠긴 항목이 이미 {len(locked)}개라 모델을 부르지 않음")
            return
        lo_eff = min(max(0 if locked else 1, lo - len(locked)), need_hi)
        line_no = {l["id"]: k for k, l in enumerate(kept, 1)}
        parts = [f"다음은 회의 {mmss(w['start_ms'])}~{mmss(w['end_ms'])} 구간의 전사야 (줄 {n}개). "
                 f"이 구간은 {'아직 진행 중이야' if is_open else '끝났어'}. 항목은 {lo_eff}~{need_hi}개 범위로 만들어줘."]
        prior = self._prior_texts(idx)
        if prior:
            parts.append("[이전 구간 요약 - 앞뒤 맥락 이해용, 근거로 쓰거나 베끼지 마]\n" + "\n".join(f"- {t}" for t in prior))
        if locked:
            rows = []
            for it in locked:
                nums = sorted(line_no[i] for i in it["line_ids"] if i in line_no)
                where = f"근거 줄 {nums[0]}~{nums[-1]}" if nums else "근거 줄이 바뀌어 번호 없음"
                rows.append(f"- ({where}) {it['text']}")
            parts.append("[사람이 확정한 항목 - 이 내용은 다시 쓰지 마]\n" + "\n".join(rows))
        parts.append("[전사]\n" + text)
        user = "\n\n".join(parts)

        covered0 = set()
        for it in locked:
            covered0.update(line_no[i] for i in it["line_ids"] if i in line_no)

        results, raws_ok, had_response = None, False, False
        cur_user = user
        zero_retry_used = False
        attempt = 0
        while True:
            attempt += 1
            if attempt > 1 + self.cfg.retries + (1 if zero_retry_used else 0):
                break   # 일반 재시도를 다 씀. '전부 탈락' 재시도는 창마다 1회만 더 허용
            if self.calls >= self.cfg.max_calls:
                self.halted = True
                self._log(f"모델 호출 상한({self.cfg.max_calls}회)에 도달해 멈춤. 창 {idx}부터는 요약하지 않음")
                break
            t0 = time.time()
            try:
                raw, err = self.llm(cur_user), None
            except Exception as e:      # 키 오류, 네트워크 오류, 사용량 제한 등
                raw, err = "", f"{type(e).__name__}: {e}"
            dt = time.time() - t0
            self.calls += 1
            w["calls"] += 1
            self.prompt_chars += len(LIVE_SYSTEM_PROMPT) + len(cur_user)
            self.call_log.append({"call": self.calls, "window": idx, "attempt": attempt, "now_ms": now,
                                  "elapsed_sec": round(dt, 2), "input_chars": len(cur_user), "error": err})
            if err:
                self.err_streak += 1
                self._log(f"창 {idx}: 모델 호출 실패 ({err[:120]})")
                if self.err_streak >= 3:
                    self.halted = True
                    self._log("연속 3회 호출에 실패해 멈춤. 키, 모델 이름, 네트워크를 확인하세요")
                    break
                continue
            self.err_streak = 0
            had_response = True
            self._save_raw(idx, self.calls, raw)
            try:
                items = parse_llm_json(raw)["summary"]
            except Exception as e:
                cur_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\nJSON 해석 실패({e}). 형식에 맞게 JSON만 다시 출력해."
                continue
            results = validate_items(items, kept)
            bad = [(i, r) for i, r in enumerate(results, 1) if r[1]]
            quality = self._window_quality(results, kept, full_stream, lo_eff, need_hi, covered0)
            raws_ok = True
            if not bad and not quality:
                break
            ok_n = sum(1 for r in results if not r[1])
            if attempt >= 1 + self.cfg.retries and not zero_retry_used and ok_n == 0 and (items or n >= 8):
                zero_retry_used = True      # 쓸 수 있는 항목이 0개 -> 오류를 알려 주고 딱 1회 더 요청
                self._log(f"창 {idx}: 쓸 수 있는 항목이 0개라 오류를 알려 주고 1회 더 요청")
            msg = "\n".join(f"- 항목 {i}: " + " / ".join(r[1]) for i, r in bad)
            qmsg = "\n".join(f"- {q}" for q in quality)
            cur_user = (user + f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg or '없음'}\n\n[품질 경고]\n{qmsg or '없음'}\n"
                        "오류가 있는 항목은 고치고, 품질 경고를 해결해서 전체 JSON을 다시 출력해.")
        if self.halted and not raws_ok:
            return
        if not had_response:
            return      # 호출 자체가 실패했으면 signature를 그대로 둬서 다음 갱신 때 다시 시도
        w["signature"], w["summarized_at_ms"] = sig, now
        if not raws_ok:
            w["notes"].append(f"{mmss(now)} 요약 실패(JSON 해석 불가). 이전 결과를 그대로 둠")
            self._log(f"창 {idx}: 요약 실패, 이전 결과 유지")
            return
        new_items, rejected = [], 0
        for it, errs, warns, spans in results:
            if errs:
                rejected += 1
                continue
            w["serial"] += 1
            line_ids = sorted({kept[k - 1]["id"] for a, b in spans for k in range(a, b + 1)})
            new_items.append({"id": f"w{idx}-{w['serial']}", "text": it["text"], "topic": it.get("topic"),
                              "evidence": spans_to_evidence(spans, kept), "line_ids": line_ids,
                              "origin": "ai", "locked": False, "edited": False, "needs_review": False,
                              "lock_sig": None, "warnings": warns, "made_at_ms": now})
        w["rejected_items"] += rejected
        old_ai = [it for it in w["items"] if not it["locked"]]
        if rejected and len(new_items) < len(old_ai):
            # 이번 결과가 검증에서 탈락해 이전보다 줄었으면 이전 항목을 지우지 않고 유지한다
            w["notes"].append(f"{mmss(now)} 새 결과 {len(new_items)}개만 통과(탈락 {rejected}개), 이전 항목 {len(old_ai)}개를 유지함")
            self._log(f"창 {idx}: 새 결과가 탈락으로 줄어 이전 항목 {len(old_ai)}개를 유지")
            new_items = old_ai
        w["items"] = sorted(locked + new_items, key=lambda it: it["evidence"][0]["start"] if it["evidence"] else 0.0)
        self._log(f"창 {idx} ({mmss(w['start_ms'])}–{mmss(w['end_ms'])}) {'진행 중' if is_open else '마감'} 요약: "
                  f"항목 {len(w['items'])}개, 호출 누계 {self.calls}")

    def _window_quality(self, results, kept, full_stream, lo, hi, covered0):
        issues = []
        ok = [r for r in results if not r[1]]
        if len(ok) < lo:
            issues.append(f"항목이 너무 적음: {len(ok)}개 (권장 {lo}~{hi}개). 화제별로 나눠야 함")
        elif len(ok) > hi:
            issues.append(f"항목이 너무 많음: {len(ok)}개 (권장 {lo}~{hi}개). 비슷한 화제는 합쳐야 함")
        covered = set(covered0)
        for n, (it, errs, warns, spans) in enumerate(results, 1):
            if errs:
                continue
            for a, b in spans:
                covered.update(range(a, b + 1))
                if b - a + 1 > MAX_SPAN_LINES:
                    issues.append(f"항목 {n}의 근거 구간 [{a},{b}]가 {b - a + 1}줄로 너무 김 (최대 {MAX_SPAN_LINES}줄)")
            tot = sum(b - a + 1 for a, b in spans)
            if tot > MAX_ITEM_LINES:
                issues.append(f"항목 {n}이 근거 {tot}줄을 한 항목으로 묶음 (최대 {MAX_ITEM_LINES}줄)")
        N, k = len(kept), 1
        while k <= N:
            if k in covered:
                k += 1
                continue
            j = k
            while j + 1 <= N and (j + 1) not in covered:
                j += 1
            if N >= 8 and j - k + 1 >= WINDOW_GAP_LINES:
                issues.append(f"줄 [{k},{j}] {j - k + 1}줄이 어느 항목에도 들어가지 않음. 내용이 있으면 항목을 추가해야 함")
            k = j + 1
        for n, (it, errs, warns, spans) in enumerate(results, 1):
            if errs:
                continue
            t = (it.get("text") or "").strip().rstrip(".")
            if GENERIC_END.search(t) and len(t) < 90:
                issues.append(f"항목 {n}이 주제만 적음(무슨 말이 나왔는지 없음): {t[:50]}…")
            if DECISION_END.search(t):
                issues.append(f"항목 {n}이 결정처럼 쓰임(제안/의견으로 바꿔야 함): {t[:50]}…")
            miss = [x for x in unknown_terms(it.get("text") or "", full_stream) if re.match(r"[a-z]", x) or len(x) >= 5]
            if miss:
                issues.append(f"항목 {n}에 지금까지 나온 전사에 없는 단어가 있음: {', '.join(miss)} -> 전사에 있는 말로 바꾸거나 빼야 함")
        return issues

    def _save_raw(self, idx, call_no, raw):
        if not self.out_dir:
            return
        d = os.path.join(self.out_dir, "llm_raw")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"win{idx:02d}_call{call_no:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(raw)


# --------------------------------------------------------------------------------------
# 2. 흘려 넣기와 결과 저장
# --------------------------------------------------------------------------------------
def replay(ls, protocol_lines, batch_sec=20):
    for b in make_batches(protocol_lines, batch_sec * 1000):
        ls.receive(b)
    ls.finish()


def latency_stats(call_log):
    xs = sorted(c["elapsed_sec"] for c in call_log)
    if not xs:
        return None
    return {"count": len(xs), "median": xs[len(xs) // 2], "max": xs[-1], "over_60s": sum(1 for x in xs if x > 60)}


def write_run_outputs(ls, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    snap_dir = os.path.join(out_dir, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    W = lambda path, s: open(path, "w", encoding="utf-8").write(s)
    W(os.path.join(out_dir, "live_state.json"), json.dumps(ls.state(), ensure_ascii=False, indent=2) + "\n")
    W(os.path.join(out_dir, "summary_end.json"), json.dumps(ls.export_summary(), ensure_ascii=False, indent=2) + "\n")
    for label, s in ls.snapshots.items():
        W(os.path.join(snap_dir, f"summary_{label}.json"), json.dumps(s, ensure_ascii=False, indent=2) + "\n")

    st = latency_stats(ls.call_log)
    lines = [f"# 회의 중 주기 요약 결과 ({ls.meeting_id})", "",
             f"- 창 길이 {ls.cfg.window_sec}초, 갱신 주기 {ls.cfg.refresh_sec}초, 창당 항목 최대 {ls.cfg.max_items}개, 재시도 {ls.cfg.retries}회",
             f"- 모델 호출 {ls.calls}회 (상한 {ls.cfg.max_calls}회){' — 상한에 걸려 중간에 멈춤' if ls.halted else ''}",
             f"- 창 {len(ls.windows)}개, 항목 {sum(len(w['items']) for w in ls.windows.values())}개, "
             f"검증 오류로 뺀 항목 {sum(w['rejected_items'] for w in ls.windows.values())}개"]
    n_err = sum(1 for c in ls.call_log if c.get("error"))
    if n_err:
        lines.append(f"- 호출 실패 {n_err}회 (live_log.txt 참고)")
    if st:
        lines.append(f"- 호출 1회 걸린 시간: 중앙값 {st['median']}초, 최대 {st['max']}초, 60초 초과 {st['over_60s']}회 "
                     f"(PRD 제안 목표는 갱신 지연 1분 이내. 이 값은 모델 호출 시간만이고 STT 지연은 포함하지 않음)")
    snap_txt = ", ".join("%s(%s)" % (k, mmss(v["now_ms"])) for k, v in ls.snapshots.items())
    lines.append("- 시점별 저장: " + snap_txt)
    lines.append("")
    for idx in sorted(ls.windows):
        w = ls.windows[idx]
        lines.append(f"## 창 {idx} ({mmss(w['start_ms'])}–{mmss(w['end_ms'])}) · {w['status']} · 사용 {w['kept_lines']}줄 · 호출 {w['calls']}회")
        for n in w["notes"]:
            lines.append(f"- 메모: {n}")
        for it in w["items"]:
            ev = ", ".join(f"{fmt_t(e['start'])}–{fmt_t(e['end'])}" for e in it["evidence"])
            flags = "".join([" [잠김]" if it["locked"] else "", " [수정됨]" if it["edited"] else "",
                             " [확인 필요]" if it["needs_review"] else ""])
            lines.append(f"- `{it['id']}` ({ev}){flags} {it['text']}")
            for wn in it["warnings"]:
                lines.append(f"  - 주의: {wn}")
        lines.append("")
    W(os.path.join(out_dir, "live_report.md"), "\n".join(lines))
    W(os.path.join(out_dir, "live_log.txt"), "\n".join(f"[{mmss(e['now_ms'])}] {e['msg']}" for e in ls.events) + "\n")


# --------------------------------------------------------------------------------------
# 3. 시점별 QA 시험 만들기
# --------------------------------------------------------------------------------------
def parse_answers(path):
    rows, header = [], None
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.split("\t")
            if header is None:
                header = cols
                continue
            d = dict(zip(header, cols))
            ev = d.get("근거 구간", "").strip()
            pairs = re.findall(r"(\d+):(\d{2})", ev)
            d["end_s"] = int(pairs[-1][0]) * 60 + int(pairs[-1][1]) if (ev != "전체" and pairs) else None
            d["no"] = int(d["번호"])
            rows.append(d)
    return rows


def eligible(q, now_ms):
    """그 시점까지 나온 말만으로 답할 수 있는 문항인지. 근거 구간이 끝난 뒤 GRACE(3초)가 지나야 포함. '전체'(답 없음 유형)는 늘 포함."""
    return q["end_s"] is None or q["end_s"] + GRACE_MS / 1000 <= now_ms / 1000


def build_qa_prompt(snapshot, questions):
    doc = snapshot["summary_doc"]
    out = [f"아래는 회의 시작부터 {mmss(snapshot['now_ms'])}까지 나온 이야기를 요약한 거예요. 이 요약만 보고 질문에 답해 주세요.", "",
           "규칙",
           '1. 요약에 없는 내용은 짐작하지 말고 "회의에서 언급 없음"이라고만 답해 주세요.',
           "2. STT가 틀린 것으로 보이는 표기도 요약에 적힌 그대로 쓰세요.",
           '3. 답은 "번호: 답" 형식으로, 질문마다 한두 문장으로 써 주세요.', "", "[요약]"]
    for k, s in enumerate(doc["summary"], 1):
        ev = ", ".join(f"{fmt_t(e['start'])}–{fmt_t(e['end'])}" for e in s["evidence"])
        out.append(f"{k}. ({ev}) {s['text']}")
    out += ["", "[질문]"]
    out += [f"{q['no']}. {q['질문']}" for q in questions]
    return "\n".join(out) + "\n"


def cmd_qa_prompts(args):
    answers = parse_answers(args.answers)
    snaps = []
    for fn in sorted(os.listdir(args.snapshots)):
        if fn.startswith("summary_") and fn.endswith(".json"):
            with open(os.path.join(args.snapshots, fn), encoding="utf-8") as f:
                snaps.append(json.load(f))
    snaps.sort(key=lambda s: s["now_ms"])
    os.makedirs(args.out, exist_ok=True)
    for s in snaps:
        if s.get("halted"):
            print(f"{s['label']:>6}: 중간에 멈춘 실행의 결과라 건너뜀 (live_report.md에서 멈춘 이유 확인)")
            continue
        qs = [q for q in answers if eligible(q, s["now_ms"])]
        with open(os.path.join(args.out, f"qa_prompt_{s['label']}.txt"), "w", encoding="utf-8") as f:
            f.write(build_qa_prompt(s, qs))
        with open(os.path.join(args.out, f"qa_grading_{s['label']}.tsv"), "w", encoding="utf-8") as f:
            f.write("번호\t유형\t질문\t정답\t근거 구간\n")
            for q in qs:
                f.write("\t".join([str(q["no"]), q.get("유형", ""), q["질문"], q.get("정답", ""), q.get("근거 구간", "")]) + "\n")
        print(f"{s['label']:>6} ({mmss(s['now_ms'])} 시점): 요약 {len(s['summary_doc']['summary'])}개 항목, 문항 {len(qs)}개 "
              f"-> qa_prompt_{s['label']}.txt, qa_grading_{s['label']}.tsv")
    print(f"\n결과 폴더: {os.path.abspath(args.out)}")


# --------------------------------------------------------------------------------------
# 4. 실행 명령
# --------------------------------------------------------------------------------------
def cmd_run(args):
    cfg = Cfg(window_sec=args.window_sec, refresh_sec=args.refresh_sec, max_items=args.max_items,
              min_open_lines=args.min_open_lines, retries=args.retries, max_calls=args.max_calls,
              context_windows=args.context_windows, provider="echo" if args.plan else args.provider, model=args.model)
    lines = load_jsonl_as_protocol_lines(args.input)
    cps = [int(x) for x in args.checkpoints.split(",") if x.strip()]
    ls = LiveSummarizer(args.meeting_id, cfg, date=args.date, out_dir=None if args.plan else args.out,
                        checkpoints_sec=cps, verbose=not args.plan)
    try:
        replay(ls, lines, args.batch_sec)
    except KeyboardInterrupt:
        print("\n중단됨. 지금까지의 결과를 저장합니다.")
    except Exception as e:
        if args.plan:
            raise
        print(f"\n오류로 멈춤: {type(e).__name__}: {e}\n지금까지의 결과를 저장합니다.")
    if args.plan:
        print_plan(ls, args)
        return
    write_run_outputs(ls, args.out)
    st = latency_stats(ls.call_log)
    print(f"\n모델 호출 {ls.calls}회, 창 {len(ls.windows)}개, 항목 {sum(len(w['items']) for w in ls.windows.values())}개")
    if st:
        print(f"호출 1회 시간: 중앙값 {st['median']}초, 최대 {st['max']}초, 60초 초과 {st['over_60s']}회")
    print(f"시점별 저장: {', '.join(ls.snapshots)}")
    print(f"결과 폴더: {os.path.abspath(args.out)}  (live_report.md를 열어 보세요)")
    print("다음: python meeting_live_summary.py qa-prompts --snapshots " + os.path.join(args.out, "snapshots")
          + " --answers qa_answers_v11.tsv --out " + os.path.join(args.out, "qa"))


def print_plan(ls, args):
    c = ls.calls
    model = args.model or "gemini-3.8-flash"
    pin, pout = PRICES.get(model, PRICES["gemini-3.8-flash"])
    tok_in = ls.prompt_chars * 2          # 한글 1글자를 토큰 2개로 넉넉히 잡은 상한 추정
    tok_out = c * 200                     # 호출당 출력 약 200토큰 가정 (생각 토큰 제외)
    usd = tok_in / 1e6 * pin + tok_out / 1e6 * pout
    print("=== 실행 계획 (모델을 부르지 않은 모의 실행 결과, 비용 0) ===")
    print(f"창 {len(ls.windows)}개 / 모델 호출 예정 {c}회 (재시도 0회 기준)")
    print(f"호출 상한 {args.max_calls}회 {'<- 계획보다 작아서 중간에 멈춤' if c > args.max_calls or ls.halted else '(계획 이내)'}")
    print(f"보내는 글자 합계 {ls.prompt_chars:,}자 (시스템 규칙 포함)")
    print(f"비용 추정(상한쪽, 추정치): 모델 {model} 기준 약 ${usd:.3f} = 약 {usd * USD_KRW_ASSUMED:,.0f}원 "
          f"(한글 1글자=토큰 2개, 출력 호출당 200토큰, 환율 {USD_KRW_ASSUMED}원 가정)")
    print("  - 모델의 생각(thinking) 토큰과 재시도는 포함하지 않음. 실제는 이보다 클 수 있어 결제 화면으로 확인해야 함")
    print("  - 재시도를 켜면(--retries 1) 검증 실패한 창만큼 호출이 늘어남")


def selftest():
    import io
    import contextlib

    def line(i, start, end, text, kind=None, low=False, arrival=None, marked=None):
        return {"id": i, "arrival_ms": arrival if arrival is not None else end, "start_ms": start, "end_ms": end,
                "type": "final", "kind": kind, "low_confidence": low, "text": text, "text_marked": marked or text}

    def meeting(minutes=7):
        out, i, t = [], 0, 1000
        while t < minutes * 60000:
            i += 1
            kind = "backchannel" if i % 9 == 0 else None
            low = i % 11 == 0
            out.append(line(i, t, t + 3500, f"알파{i}번 이야기 베타{i}", kind=kind, low=low,
                            marked=f"⟨알파{i}번 이야기 베타{i}⟩" if low else None))
            t += 6000
        return out

    def run_meeting(cfg, edits_at=None, checkpoints=(), lock=None, llm=None):
        ls = LiveSummarizer("t", cfg, llm=llm, checkpoints_sec=checkpoints, verbose=False)
        calls_before = []
        for b in make_batches(meeting(), 20000):
            if edits_at and b["sent_ms"] == edits_at[0]:
                b["edits"] = edits_at[1]
            ls.receive(b)
            if lock and b["sent_ms"] == lock[0]:
                lock[1](ls)
            calls_before.append(ls.calls)
        ls.finish()
        return ls

    # 1) 창 나누기와 확정 시점
    ls = run_meeting(Cfg())
    assert ls.windows[0]["status"] == "sealed" and ls.windows[3]["status"] == "sealed"
    ids0 = {it["id"] for it in ls.windows[0]["items"]}
    assert ids0 and all(i.startswith("w0-") for i in ids0)
    # 모든 항목에 근거가 있고 근거가 그 창 안의 줄에서 나옴
    for w in ls.windows.values():
        for it in w["items"]:
            assert it["evidence"] and it["line_ids"]
            for e in it["evidence"]:
                assert e["end"] * 1000 >= w["start_ms"] and e["start"] * 1000 < w["end_ms"] + 20000
    # 2) 저장 형식 v0.1 그대로 내보내기
    doc = ls.export_summary()
    assert set(doc) == {"schema_version", "meeting_id", "date", "summary", "decisions", "action_items", "extensions"}
    assert all(set(s) == {"text", "topic", "evidence"} for s in doc["summary"])
    assert doc["decisions"] == [] and doc["action_items"] == [] and doc["extensions"] is None
    starts = [s["evidence"][0]["start"] for s in doc["summary"]]
    assert starts == sorted(starts), "항목이 시간 순이 아님"
    # 3) 호출 수: 창 7개(7분, 120초 단위) x 대략 2번 + 마지막 마감. 과하게 늘지 않아야 함
    assert ls.calls <= 2 * len(ls.windows) + 2, (ls.calls, len(ls.windows))
    # 4) 확정된 창은 STT 수정이 와야만 다시 요약된다
    base_calls = ls.calls
    ls2 = run_meeting(Cfg(), edits_at=(260000, [{"id": 5, "rev": 1, "text": "알파5번 고친 이야기", "text_marked": "알파5번 고친 이야기"}]))
    assert ls2.calls == base_calls + 1, (ls2.calls, base_calls)   # 확정된 창 하나만 추가 호출
    # 5) 잠금: 사람이 고친 항목은 갱신 뒤에도 그대로, 근거 줄이 바뀌면 확인 필요
    state = {}

    def do_lock(ls_):
        it = ls_.windows[0]["items"][0]
        state["id"], state["line"] = it["id"], it["line_ids"][0]
        ls_.lock_item(it["id"], new_text="사람이 고친 문장임")
    ls3 = run_meeting(Cfg(), lock=(140000, do_lock),
                      edits_at=(320000, [{"id": 1, "rev": 1, "text": "알파1번 바뀐 말", "text_marked": "알파1번 바뀐 말"}]))
    w, it = ls3.find_item(state["id"])
    assert it["locked"] and it["text"] == "사람이 고친 문장임" and it["edited"]
    assert it["needs_review"] == (state["line"] == 1), (it["needs_review"], state)
    # 6) 호출 상한
    ls4 = run_meeting(Cfg(max_calls=3))
    assert ls4.halted and ls4.calls == 3
    # 7) 시점별 저장
    ls5 = run_meeting(Cfg(), checkpoints=(180, 300))
    assert set(ls5.snapshots) == {"3min", "5min", "end"}, set(ls5.snapshots)
    assert ls5.snapshots["3min"]["now_ms"] <= 180000 < ls5.snapshots["end"]["now_ms"]
    n3 = len(ls5.snapshots["3min"]["summary_doc"]["summary"])
    n5 = len(ls5.snapshots["5min"]["summary_doc"]["summary"])
    ne = len(ls5.snapshots["end"]["summary_doc"]["summary"])
    assert 0 < n3 <= n5 <= ne, (n3, n5, ne)
    # 8) 항목 개수 범위
    assert window_range(0, 3) == (0, 0) and window_range(3, 3) == (1, 1) and window_range(8, 3) == (2, 3)
    # 9) 시점별 QA 문항 고르기
    import tempfile
    tsv = ("# 주석\n번호\t유형\t질문\t정답\t근거 구간\t난이도\t검수 메모\n"
           "1\t사실\t질문1\t답1\t02:11–02:16\t하\t\n2\t사실\t질문2\t답2\t09:00–09:30\t하\t\n"
           "3\t답 없음\t질문3\t회의에서 언급 없음\t전체\t상\t\n")
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8") as tf:
        tf.write(tsv)
    rows = parse_answers(tf.name)
    os.unlink(tf.name)
    assert [q["no"] for q in rows if eligible(q, 600000)] == [1, 2, 3]
    assert [q["no"] for q in rows if eligible(q, 560000)] == [1, 3]
    assert [q["no"] for q in rows if eligible(q, 139000)] == [1, 3]
    assert [q["no"] for q in rows if eligible(q, 138000)] == [3]
    prompt = build_qa_prompt({"now_ms": 600000, "summary_doc": doc}, [q for q in rows if q["no"] == 1])
    assert "[요약]" in prompt and "1. 질문1" in prompt and "답1" not in prompt
    # 9-2) 호출 실패: 한 번 실패하면 다음 갱신 때 다시 시도, 연속 3번 실패하면 멈춤
    cnt = {"n": 0}

    def flaky(user):
        cnt["n"] += 1
        if cnt["n"] == 1:
            raise RuntimeError("일시 오류")
        return echo_llm(user)

    ls6 = run_meeting(Cfg(), llm=flaky)
    assert not ls6.halted and ls6.windows[0]["items"], "일시 오류 뒤 다시 시도하지 못함"
    assert sum(1 for c in ls6.call_log if c["error"]) == 1

    def broken(user):
        raise RuntimeError("키 오류")

    ls7 = run_meeting(Cfg(), llm=broken)
    assert ls7.halted and ls7.calls == 3 and not any(w["items"] for w in ls7.windows.values())
    assert all(w["status"] == "open" for w in ls7.windows.values()), "요약에 실패한 창을 닫아 버림"
    assert ls7.snapshots["end"]["halted"] is True
    # 10) 시스템 규칙에 구간용 규칙이 들어갔는지
    assert "한 구간이야" in LIVE_SYSTEM_PROMPT and "출력은 JSON만" in LIVE_SYSTEM_PROMPT
    print("자체 시험 통과 (창 나누기, 확정, 수정 반영, 잠금과 확인 필요, 호출 상한, 호출 실패 처리, 시점별 저장, QA 문항 선별)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="회의 중 주기 요약")
    ap.add_argument("--selftest", action="store_true", help="자체 시험만 실행 (비용 없음)")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="전사를 흘려 넣으며 주기 요약 실행")
    r.add_argument("--input", default="meeting1_full_transcript.jsonl", help="STT 전사 jsonl")
    r.add_argument("--out", default="out_live", help="결과 폴더")
    r.add_argument("--provider", default="echo", choices=["echo", "gemini", "anthropic", "openai"],
                   help="echo는 모델을 부르지 않는 모의 실행(비용 0). 기본값")
    r.add_argument("--model", default=None, help="모델 ID (gemini, openai는 필수, anthropic은 생략하면 claude-sonnet-5-5)")
    r.add_argument("--meeting-id", default="m_sample")
    r.add_argument("--date", default=datetime.date.today().isoformat())
    r.add_argument("--window-sec", type=int, default=120, help="창 길이(초)")
    r.add_argument("--refresh-sec", type=int, default=60, help="갱신 주기(초)")
    r.add_argument("--batch-sec", type=int, default=20, help="흘려 넣는 묶음 간격(초)")
    r.add_argument("--max-items", type=int, default=3, help="창 하나에서 만들 항목 최대 개수")
    r.add_argument("--min-open-lines", type=int, default=4, help="진행 중인 창은 이 줄 수가 모이면 요약 시작")
    r.add_argument("--retries", type=int, default=0, help="검증 실패 때 다시 요청하는 횟수 (늘리면 호출도 늘어남)")
    r.add_argument("--max-calls", type=int, default=60, help="모델 호출 상한 (넘으면 멈춤)")
    r.add_argument("--context-windows", type=int, default=2, help="맥락용으로 주는 이전 구간 수")
    r.add_argument("--checkpoints", default="600,1200", help="시점별로 저장할 회의 경과 초 (쉼표로 구분). 종료 시점은 항상 저장")
    r.add_argument("--plan", action="store_true", help="모델을 부르지 않고 호출 횟수와 비용 추정만 출력")

    q = sub.add_parser("qa-prompts", help="시점별 요약 + 그 시점까지의 문항으로 QA 프롬프트 만들기")
    q.add_argument("--snapshots", default=os.path.join("out_live", "snapshots"))
    q.add_argument("--answers", default="qa_answers_v11.tsv")
    q.add_argument("--out", default=os.path.join("out_live", "qa"))

    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if args.cmd == "run":
        load_dotenv(os.path.join(HERE, "..", ".env"))   # API 키를 .env 에서 읽는다 (값은 출력하지 않는다)
        if args.provider == "anthropic" and not args.model:
            args.model = "claude-sonnet-5-5"
        if args.provider in ("gemini", "openai") and not args.model and not args.plan:
            ap.error(f"--provider {args.provider}는 --model(모델 ID)이 필요합니다")
        cmd_run(args)
    elif args.cmd == "qa-prompts":
        cmd_qa_prompts(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
