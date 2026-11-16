#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
종료 처리 시간 재기 (손으로 시험하는 도구. 서버를 띄우지 않는다)

끝난 회의 전사를 실제 서버 입구(Registry)에 20초 묶음으로 흘려 보내고, 종료 신호를 보낸 뒤 "최종 보드(todaySummary, final)"가 나오기까지의 시간을 잰다.
회의록 초안은 이 최종 보드가 meeting-service 에 저장돼야(ready) 시작하므로, 이 시간이 회의록 생성의 1단계(재료 준비)다.
minutes-service 는 재료를 최대 10분 기다린다(source-wait). 이 시간이 600초에 가까우면 DRAFT_SOURCE_TIMEOUT 이 난다.

재는 것
  - 종료 신호 -> 최종 보드까지 걸린 시간 (핵심)
  - 묶음을 보낼 때마다 대기열에 쌓인 일감 수(queued)와 처리가 뒤처진 정도
  - 처리 한 번(패스)에 걸린 시간 (묶음 패스와 종료 패스), 묶음을 합쳐서 처리한 횟수
  - 모델 호출 수, 오류 수
--draft 를 주면 최종 보드로 회의록 초안 요청을 만들어 이어서 초안까지 만든다 (minutes-service 가 하는 일을 흉내). 오인식 교정, 칸 배치, 표, 문장 다듬기의
호출 시간과 토큰을 호출마다 적고, "종료 -> 회의록 초안까지" 전체 시간을 낸다. 초안 AI 설정은 MINUTES_LLM_* (--dev 가 DEV 값으로 채움), MINUTES_CORRECTION=0 / MINUTES_POLISH=0 도 --set 으로 쓸 수 있다.
백엔드(meeting-service, minutes-service)와 네트워크는 없다. 보드는 가짜 publisher 가 받아 시각만 적는다 (실제 publisher 의 전송 간격 제한이 없어 조금 낙관적이다).
회의록 초안 생성(2단계)은 python -m minutes.try_draft 로 따로 잰다.

사용 (저장소 루트에서)
  # 모델 호출 없이 배선만 점검 (비용 0, 빨리 감기)
  python -m server.try_timing --transcript data/eval_v2/a_1014_mentor.jsonl --agendas data/eval_v2/a_agendas.json --speed 0
  # 실제 모델, 실시간 속도 (유료. 회의 길이만큼 걸린다). DEV 와 같은 설정은 --dev
  python -m server.try_timing --transcript ... --agendas ... --provider openai --model gpt-6-luna --dev --paid-ok
  # 앞 5분만 써서 비용을 줄인다 (회의 길이에 비례해 호출 수가 늘어난다)
  python -m server.try_timing ... --provider openai --model gpt-6-luna --dev --paid-ok --max-minutes 5
  # 호출 수와 비용만 어림 (아무것도 호출하지 않는다)
  python -m server.try_timing ... --provider openai --model gpt-6-luna --dev --max-minutes 5 --draft --estimate-only
  # 회의록 초안까지 이어서 (유료, 파이프라인 + 초안 호출)
  python -m server.try_timing ... --provider openai --model gpt-6-luna --dev --paid-ok --max-minutes 5 --draft
  # 설정을 바꿔 비교 (여러 번 쓸 수 있다)
  python -m server.try_timing ... --provider openai --model gpt-6-luna --paid-ok --set AGENDA_PARALLEL=1 --set BATCH_COALESCE_MAX=8

--speed 1 은 실제 도착 속도(기본), 10 이면 10배 빠르게 보내고, 0 이면 기다리지 않고 한꺼번에 보낸다(처리 능력의 한계를 본다).
키는 환경변수나 저장소 루트 .env 에서 읽는다. 전사 내용은 출력하지 않는다.
"""
import argparse
import inspect
import json
import logging
import os
import re
import statistics
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server import service as svc  # noqa: E402  (summary/ 를 sys.path 에 넣는다)
from server.publisher import AGENDA_BOARD  # noqa: E402
from meeting_summary import load_jsonl_as_protocol_lines, make_batches  # noqa: E402
from minutes.draft import make_draft  # noqa: E402
from minutes.materials import build_materials  # noqa: E402
from minutes.llm_config import make_minutes_llm  # noqa: E402
from minutes.request import validate_request, InvalidInput  # noqa: E402
from minutes.correction import CORRECTION_KIND  # noqa: E402
from minutes.placement import load_prompt  # noqa: E402
from minutes.polish import POLISH_KIND  # noqa: E402
from minutes.table import TABLE_KIND  # noqa: E402
from minutes.try_draft import PlaceAllFakeLLM  # noqa: E402
from shared.envfile import local_env  # noqa: E402
from shared.llm import LLMConfigError  # noqa: E402

MID = "timing"
SOURCE_WAIT_SEC = 600            # minutes-service 의 source-wait (10분)
DEV_SETTINGS = {                 # nineouting-infra 의 DEV values 에 들어 있는 값 (2026-11-13 기준)
    "AGENDA_SUMMARY_PROMPT": "v8", "AGENDA_PARALLEL": "1", "BATCH_COALESCE_MAX": "4",
    "LLM_TIMEOUT_SEC": "120", "LLM_MAX_RETRIES": "1",
    "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "gpt-5.6-luna",      # 회의록 초안 AI (--draft 일 때만 쓴다)
}
SHOWN = ("MINUTES_LLM_PROVIDER", "MINUTES_LLM_MODEL", "MINUTES_CORRECTION", "MINUTES_POLISH", "MINUTES_POLISH_CHUNK", "AGENDA_PARALLEL", "BATCH_COALESCE_MAX", "LLM_TIMEOUT_SEC", "LLM_MAX_RETRIES", "AGENDA_SUMMARY_PROMPT",
         "LIVE_SUMMARY_PROMPT", "AGENDA_OPINION_PROMPT", "AGENDA_TODO_PROMPT")


class CapturePublisher:
    """백엔드 대신 받는 가짜 publisher. 최종 보드(todaySummary 가 있고 final)가 온 시각을 적는다."""
    enabled = True

    def __init__(self):
        self.lock = threading.Lock()
        self.boards = 0
        self.final_at = None             # 최종 보드가 처음 온 시각 (monotonic)
        self.first_today_at = None       # todaySummary 가 처음 담긴 보드가 온 시각 (종료 버튼 요청 때 오는 값 포함)
        self.final_ts = None             # 최종 todaySummary (--draft 가 초안 요청을 만들 때 쓴다)

    def submit(self, meeting_id, payload, kind=None):
        if kind != AGENDA_BOARD:
            return
        now = time.monotonic()
        ts = payload.get("todaySummary") if isinstance(payload, dict) else None
        with self.lock:
            self.boards += 1
            if ts:
                self.first_today_at = self.first_today_at or now
                if ts.get("final") and self.final_at is None:
                    self.final_at = now
                    self.final_ts = json.loads(json.dumps(ts))

    def info(self):
        return {"enabled": True, "boards": self.boards}


def load_dotenv(path):
    """저장소 루트 .env 를 읽는다. 이미 있는 환경변수는 덮어쓰지 않는다."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k and v and k not in os.environ:
                os.environ[k] = v


def to_backend_line(ln):
    """내부 줄 형식 -> 백엔드가 보내는 줄 형식 (examples/api/lines-batch-basic.json)."""
    return {"seq": ln["id"], "startMs": ln["start_ms"], "endMs": ln["end_ms"], "text": ln["text"], "textMarked": ln["text_marked"],
            "kind": None if ln["kind"] in (None, "None", "") else ln["kind"], "lowConfidence": ln["low_confidence"],
            "recovered": ln["type"] == "recovered", "manual": bool(ln.get("manual", False))}


class TimedLLM:
    """초안 AI 호출마다 걸린 시간과 토큰을 기록한다."""

    def __init__(self, llm):
        self.llm, self.calls = llm, []
        self.name, self.model = llm.name, llm.model
        # 지시문 글로 호출 종류를 가린다 (서버와 같은 기본 버전)
        self.stages = {(load_prompt() if kind is None else load_prompt(kind=kind)).text: label
                       for label, kind in (("칸 배치", None), ("오인식 교정", CORRECTION_KIND), ("문장 다듬기", POLISH_KIND), ("표 채우기", TABLE_KIND))}

    def generate(self, system, user, timeout_s, **kw):
        t0 = time.monotonic()
        try:
            reply = self.llm.generate(system, user, timeout_s, **kw)
        except Exception:                                  # 시간 초과 등: 걸린 시간만 적고 그대로 올린다
            self.calls.append({"sec": time.monotonic() - t0, "in": None, "out": None, "stage": self.stages.get(system, "기타") + " (실패)", "error": True})
            raise
        self.calls.append({"sec": time.monotonic() - t0, "in": reply.input_tokens, "out": reply.output_tokens,
                           "stage": self.stages.get(system, "기타")})
        return reply


def build_draft_request(final_ts, agendas_msg, template_from):
    """최종 todaySummary 로 minutes-service 가 보내는 초안 요청을 만든다. 양식·참석자는 템플릿 요청 파일에서 가져온다.
    안건 번호가 양의 정수가 아니면(평가용 파일의 'a1' 같은 값) 순서대로 1, 2, ... 로 바꾼다."""
    with open(template_from, encoding="utf-8") as f:
        base = json.load(f)
    ids, agendas = {}, []
    for i, a in enumerate(agendas_msg["agendas"], 1):
        raw = a["agendaId"]
        new = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else i
        ids[raw] = new
        ent = {"agendaId": new, "title": a["title"]}
        if a.get("decisionPoints"):
            ent["decisionPoints"] = [{"decisionPointId": p["decisionPointId"], "position": p.get("position", j), "text": p["text"]}
                                     for j, p in enumerate(a["decisionPoints"], 1)]
        agendas.append(ent)
    summary = json.loads(json.dumps(final_ts))
    for a in summary.get("agendas", []):
        a["agendaId"] = ids.get(a["agendaId"], a["agendaId"])
    return {"requestId": "try-timing-draft", "minutesId": 1, "meetingId": 1, "title": "시험 회의", "meetingStartAt": "2026-01-01T10:00:00",
            "attendees": base.get("attendees", []), "template": base["template"], "agendas": agendas, "summary": summary,
            "callbackUrl": "http://localhost/none"}


def run_draft(body, provider):
    """초안을 만들고 결과를 요약한다. provider 가 echo 면 가짜 AI(비용 0), 아니면 MINUTES_LLM_* 로 실제 AI."""
    env = local_env()
    if provider == "echo":
        llm, timeout_s = PlaceAllFakeLLM(), 60.0
    else:
        try:
            llm, timeout_s = make_minutes_llm(env)
        except LLMConfigError as e:
            sys.exit(f"회의록 초안 AI 설정 오류: {e} (--dev 를 쓰거나 MINUTES_LLM_PROVIDER, MINUTES_LLM_MODEL 을 넣으세요)")
        if llm.name == "fake":
            sys.exit("MINUTES_LLM_PROVIDER 가 fake 입니다. openai 나 gemini 를 넣어야 실제 초안 AI 로 돕니다")
    correction = env.get("MINUTES_CORRECTION", "1") != "0"
    polish = env.get("MINUTES_POLISH", "1") != "0"
    polish_chunk = int(env.get("MINUTES_POLISH_CHUNK") or 0)
    supported = "polish_chunk" in inspect.signature(make_draft).parameters          # 다듬기 조각 기능이 없는 코드에서도 돈다
    if polish_chunk and not supported:
        print("주의: 이 코드에는 다듬기 조각 기능(MINUTES_POLISH_CHUNK)이 없어 무시합니다")
        polish_chunk = 0
    extra = {"polish_chunk": polish_chunk} if supported else {}
    out = {"llm": f"{llm.name}/{llm.model}", "correction": correction, "polish": polish, "polishChunk": polish_chunk}
    try:
        validate_request(body)
        out["requestCheck"] = "통과"
    except InvalidInput as e:
        out.update(requestCheck="거절", requestError=str(e), status="FAILED", errorCode="INVALID_INPUT", sec=0.0, calls=[])
        return out
    timed = TimedLLM(llm)
    t0 = time.monotonic()
    result = make_draft(body, timed, timeout_s=timeout_s, correction=correction, polish=polish, **extra)
    out.update(sec=time.monotonic() - t0, status=result["status"], calls=timed.calls)
    if result["status"] == "SUCCEEDED":
        out["sections"] = len(result["sections"])
        out["unplaced"] = len(result["unplaced"])
    else:
        out["errorCode"] = result["error"]["code"]
        out["errorMessage"] = result["error"]["message"]
    return out


def estimate(minutes, draft):
    """호출 수와 비용 어림. 5분 실측(DEV 설정, 호출 42회, 약 200원)과 이전 평가 단가(호출당 3-7원)를 길이에 비례시킨 값이다."""
    calls = round(8.4 * minutes)
    krw = calls * 5
    if draft:
        calls, krw = calls + 7, krw + 60                   # 초안: 교정 1, 배치 1, 다듬기 1-2, 표 필드마다 1 (대략 5-8회, 호출당 약 9원)
    return calls, krw


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0.0


def fmt(sec):
    return f"{sec:.1f}초 ({int(sec // 60)}분 {sec % 60:.0f}초)"


def main():
    ap = argparse.ArgumentParser(description="종료 처리 시간 재기 (회의록 생성 1단계)")
    ap.add_argument("--transcript", required=True, help="전사 jsonl (data/eval_v2/a_1014_mentor.jsonl 같은 평가용 파일)")
    ap.add_argument("--agendas", required=True, help="안건 목록 JSON (agendas-put 형식)")
    ap.add_argument("--speed", type=float, default=1.0, help="1=실시간(기본), 10=10배 빠르게, 0=기다리지 않고 한꺼번에")
    ap.add_argument("--period", type=int, default=20, help="묶음 간격(초). 실제 STT 는 20초")
    ap.add_argument("--max-minutes", type=float, help="전사의 앞쪽 이 분량만 쓴다 (비용을 줄인다. 호출 수와 걸리는 시간이 길이에 비례한다)")
    ap.add_argument("--provider", help="LLM_PROVIDER 로 쓸 값 (기본: 환경변수, 없으면 echo)")
    ap.add_argument("--model", help="LLM_MODEL")
    ap.add_argument("--dev", action="store_true", help="DEV 와 같은 설정(AGENDA_PARALLEL=1 등)을 쓴다. .env 와 환경변수의 같은 이름 값을 덮어쓴다")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VAL", help="환경변수를 정한다 (여러 번). --dev 보다 우선")
    ap.add_argument("--draft", action="store_true", help="최종 보드로 회의록 초안 요청을 만들어 초안까지 이어서 만든다 (2단계)")
    ap.add_argument("--template-from", default=os.path.join(ROOT, "minutes", "tests", "fixtures", "draft_request.json"),
                    help="--draft 가 양식·참석자를 가져올 요청 JSON (기본: 합성 요청. 표 필드를 보려면 table_request.json)")
    ap.add_argument("--request-out", help="--draft 가 만든 초안 요청을 이 경로에 저장한다. 요약 내용이 들어 있으니 커밋하지 않는 폴더(data/)에 둔다. "
                    "저장한 요청은 python -m minutes.try_draft --file ... --real 로 초안만 싸게 다시 시험할 수 있다")
    ap.add_argument("--estimate-only", action="store_true", help="호출 수와 비용만 어림해서 보여 주고 아무것도 호출하지 않는다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (echo 가 아닐 때 필요)")
    ap.add_argument("--timeout", type=float, default=1800.0, help="종료 신호 뒤 최종 보드를 기다리는 최대 초")
    ap.add_argument("--out", help="결과 JSON 경로 (숫자만, 전사 내용 없음)")
    a = ap.parse_args()

    overrides = {}
    for item in a.set:
        if "=" not in item:
            sys.exit(f"--set 은 KEY=VAL 형식이어야 합니다: {item}")
        k, v = item.split("=", 1)
        overrides[k.strip()] = v.strip()
    load_dotenv(os.path.join(ROOT, ".env"))
    if a.provider:
        os.environ["LLM_PROVIDER"] = a.provider
    if a.model:
        os.environ["LLM_MODEL"] = a.model
    if a.dev:
        os.environ.update(DEV_SETTINGS)
    os.environ.update(overrides)
    provider = os.environ.get("LLM_PROVIDER", "echo")
    if a.estimate_only:
        pass                                               # 아래에서 길이를 안 뒤 어림만 하고 끝낸다
    elif provider != "echo" and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (모델 없이 점검하려면 --provider echo)")

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=a.period * 1000)
    if a.max_minutes:
        limit = int(a.max_minutes * 60000)
        batches = [b for b in batches if b["sent_ms"] <= limit]
        if not batches:
            sys.exit("--max-minutes 가 너무 작아 쓸 묶음이 없습니다")
        lines = [ln for b in batches for ln in b["lines"]]
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    agendas_msg["meetingId"] = MID
    duration = batches[-1]["sent_ms"] / 1000
    est_calls, est_krw = estimate(duration / 60, a.draft)
    if provider != "echo" or a.estimate_only:
        print(f"어림: 모델 호출 약 {est_calls}회, 약 {est_krw:,}원 (5분 실측 42회를 길이에 비례시킨 값. 실제와 다를 수 있음)")
    if a.estimate_only:
        sys.exit(0)

    # 처리 한 번(패스)의 시간과 합쳐서 처리한 횟수를 잰다 (서버 코드는 건드리지 않고 감싼다)
    passes, merged = [], []
    orig_with_live = svc.Registry._with_live

    def timed_with_live(self, m, name, live_fn, rest_fn):
        t0 = time.monotonic()
        try:
            return orig_with_live(self, m, name, live_fn, rest_fn)
        finally:
            passes.append((name, t0, time.monotonic() - t0))
    svc.Registry._with_live = timed_with_live

    class Count(logging.Handler):
        def emit(self, r):
            msg = r.getMessage()
            if "밀린 묶음을 합쳐서 처리" in msg:
                n = re.search(r"batches=(\d+)", msg)
                merged.append(int(n.group(1)) if n else 0)
    lg = logging.getLogger("ai-service")
    lg.setLevel(logging.INFO)
    lg.propagate = False                                   # 로그는 화면에 쏟지 않는다 (개수만 센다)
    lg.addHandler(Count())

    cap = CapturePublisher()
    reg = svc.Registry(svc.cfg_from_env(), publisher=cap)
    reg.receive_event(MID, {"meetingId": MID, "event": "start", "atMs": 0})
    reg.receive_agendas(MID, agendas_msg)

    shown = {k: os.environ[k] for k in SHOWN if k in os.environ}
    print(f"전사 {len(lines)}줄, 길이 약 {fmt(duration)}, 묶음 {len(batches)}개({a.period}초 간격)")
    print(f"재생: {'한꺼번에' if a.speed == 0 else ('실시간' if a.speed == 1 else f'{a.speed:g}배 빠르게')} / provider={provider} / 설정={shown or '기본값'}")

    start = time.monotonic()
    queued_log, last_report = [], 0.0
    for b in batches:
        if a.speed > 0:
            wait = start + (b["sent_ms"] / 1000) / a.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        msg = {"meetingId": MID, "batch": b["seq"], "lines": [to_backend_line(x) for x in b["lines"]], "edits": []}
        r = reg.receive_batch(MID, msg)
        if r.get("result") != "accepted":
            sys.exit(f"묶음 {b['seq']} 를 받지 못함: {r}")
        st = reg.status(MID)
        queued_log.append((b["seq"], time.monotonic() - start, st["queued"]))
        now = time.monotonic() - start
        if now - last_report >= 30 or b["seq"] == len(batches):
            last = passes[-1][2] if passes else 0.0
            print(f"  [{now:6.0f}초] 묶음 {b['seq']}/{len(batches)} 전송, 대기 {st['queued']}개, 직전 처리 {last:.0f}초")
            last_report = now

    st = reg.status(MID)
    queued_at_end = st["queued"]
    t_end = time.monotonic()
    reg.receive_event(MID, {"meetingId": MID, "event": "end", "atMs": batches[-1]["sent_ms"]})
    print(f"종료 신호를 보냄 (그때 대기 {queued_at_end}개). 최종 보드를 기다립니다 (최대 {a.timeout:.0f}초)")
    while cap.final_at is None and time.monotonic() - t_end < a.timeout:
        time.sleep(0.5)
    reg.wait_idle(MID, timeout=5)

    final_sec = (cap.final_at - t_end) if cap.final_at else None
    batch_passes = [d for n, _t, d in passes if n == "batch"]
    end_passes = [d for n, _t, d in passes if n == "end"]
    stt = reg.status(MID)
    calls = {"summary": stt["llmCalls"], "classify": stt["agendaClassification"].get("calls"), "status": stt["agendaStatus"].get("calls"),
             "agendaSummary": stt["agendaSummary"].get("calls"), "opinions": stt["agendaOpinions"].get("calls"),
             "todos": stt["agendaTodos"].get("calls")}
    result = {
        "transcriptLines": len(lines), "durationSec": round(duration, 1), "batches": len(batches), "speed": a.speed, "provider": provider,
        "settings": shown,
        "endSignalToFinalBoardSec": None if final_sec is None else round(final_sec, 1),
        "overSourceWait": None if final_sec is None else final_sec > SOURCE_WAIT_SEC,
        "queuedAtEndSignal": queued_at_end, "maxQueued": max((q for _s, _t, q in queued_log), default=0),
        "batchPasses": {"n": len(batch_passes), "meanSec": round(statistics.mean(batch_passes), 1) if batch_passes else 0,
                        "p95Sec": round(pct(batch_passes, 0.95), 1), "maxSec": round(max(batch_passes, default=0), 1)},
        "endPassSec": [round(x, 1) for x in end_passes], "mergedPasses": len(merged), "mergedBatchesTotal": sum(merged),
        "llmCalls": calls, "errors": stt["errors"], "boards": cap.boards,
    }
    print("\n=== 결과 (숫자만, 전사 내용 없음) ===")
    if final_sec is None:
        print(f"① 종료 신호 -> 최종 보드: {a.timeout:.0f}초 안에 나오지 않음 (10분 한도 {SOURCE_WAIT_SEC}초 초과)")
    else:
        flag = "  <- 10분 한도 초과 (DRAFT_SOURCE_TIMEOUT 이 난다)" if final_sec > SOURCE_WAIT_SEC else ""
        print(f"① 종료 신호 -> 최종 보드(재료 준비): {fmt(final_sec)}{flag}")
    print(f"대기열: 종료 신호 때 {queued_at_end}개, 재생 중 최대 {result['maxQueued']}개")
    bp = result["batchPasses"]
    print(f"묶음 처리(패스) {bp['n']}회: 평균 {bp['meanSec']}초, p95 {bp['p95Sec']}초, 최대 {bp['maxSec']}초 (묶음은 {a.period}초마다 옴)")
    print(f"종료 패스: {result['endPassSec']}초, 묶음을 합쳐서 처리한 패스 {len(merged)}회(합쳐진 묶음 {sum(merged)}개)")
    print(f"모델 호출 수: {calls}, 오류 {stt['errors']}")
    if a.draft:
        if cap.final_ts is None:
            print("\n최종 보드가 없어 초안을 만들지 않습니다")
        else:
            body = build_draft_request(cap.final_ts, agendas_msg, a.template_from)
            if a.request_out:
                os.makedirs(os.path.dirname(os.path.abspath(a.request_out)), exist_ok=True)
                with open(a.request_out, "w", encoding="utf-8") as f:
                    json.dump(body, f, ensure_ascii=False, indent=2)
                print(f"초안 요청을 {a.request_out} 에 저장했습니다 (요약 내용 포함, 커밋 금지)")
            print("\n=== 회의록 초안 (2단계) ===")
            d = run_draft(body, provider)
            result["draft"] = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in d.items() if k != "calls"}
            result["draft"]["aiCalls"] = [{"sec": round(c["sec"], 1), "in": c["in"], "out": c["out"], "stage": c["stage"]} for c in d["calls"]]
            try:
                n_mat = len(build_materials(body["agendas"], body["summary"]))
            except Exception:                              # 재료 수는 참고용이라 못 세도 계속한다
                n_mat = None
            d["materials"] = n_mat
            result["draft"]["materials"] = n_mat
            print(f"초안 AI: {d['llm']} (교정 {'켬' if d['correction'] else '끔'}, 다듬기 {'끔' if not d['polish'] else (str(d['polishChunk']) + '줄씩 나눠' if d['polishChunk'] else '켬')}) / 요청 검사: {d['requestCheck']}")
            if d["requestCheck"] == "거절":
                print(f"요청 형식 거절(INVALID_INPUT): {d['requestError']}")
            else:
                tin = sum(c["in"] or 0 for c in d["calls"])
                tout = sum(c["out"] or 0 for c in d["calls"])
                res = d["status"] + ("" if d["status"] == "SUCCEEDED" else f" ({d['errorCode']}: {d['errorMessage']})")
                print(f"② 초안 생성: {fmt(d['sec'])} / 결과 {res} / 재료 {d['materials']}개 / AI 호출 {len(d['calls'])}회 (입력 {tin}, 출력 {tout} 토큰)")
                for i, c in enumerate(d["calls"], 1):
                    tok = "" if c["in"] is None else f", 입력 {c['in']}, 출력 {c['out']} 토큰"
                    print(f"   {i}. {c['sec']:.1f}초 ({c['stage']}){tok}")
                if d["status"] == "SUCCEEDED":
                    print(f"   칸 {d['sections']}개, 넣지 못한 재료 {d['unplaced']}개")
                if final_sec is not None:
                    total = final_sec + d["sec"]
                    result["endToDraftSec"] = round(total, 1)
                    print(f"종료 -> 회의록 초안까지(① + ②): {fmt(total)}  (minutes-service 의 10초 확인 주기와 네트워크는 빠짐)")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"결과를 {a.out} 에 저장했습니다")


if __name__ == "__main__":
    main()
