#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
끝난 회의 요약: 끝난 회의의 전사 전체를 LLM에 한 번 넣어 summary[]를 만든다.

흐름
  1) TranscriptStore  : STT 전송 묶음(lines / edits)을 받아 최신 전사 상태를 유지
  2) build_input      : kind(맞장구·간투사)와 low_confidence 줄을 빼고 [번호 시각] 텍스트로 만듦
  3) call_llm         : LLM 호출 (mock / gemini / anthropic / openai)
  4) validate         : 근거 줄 번호, 인용, STT 불확실 표시, 숫자를 코드로 검사
  5) 출력             : summary.json(저장 형식 v0.1), report.md, validation.txt 등

표준 라이브러리만 쓰고, 실제 LLM을 부를 때만 해당 SDK가 필요하다.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HEDGE = re.compile(r"STT|불확실|불명확")          # 프롬프트 규칙 4와 같은 말
RECOVERED_DELAY_MS = 2000                          # recovered 줄은 말소리 끝 약 2초 뒤 도착 (규격 문서)

# --------------------------------------------------------------------------------------
# 0. 공통 도구
# --------------------------------------------------------------------------------------
def mmss(ms):
    s = int(ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def squash(s):
    """비교용: 공백과 문장부호를 뺀다."""
    return re.sub(r"[\s\.\?\!,]", "", s)


def kor_to_int(phrase):
    """'삼십 오만 칠천 삼백 오십 팔' -> 357358"""
    d = {"영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
    u = {"십": 10, "백": 100, "천": 1000}
    total = section = num = 0
    for ch in phrase.replace(" ", ""):
        if ch in d:
            num = d[ch]
        elif ch in u:
            section += (num or 1) * u[ch]
            num = 0
        elif ch == "만":
            total += ((section + num) or 1) * 10000
            section = num = 0
    return total + section + num


# --------------------------------------------------------------------------------------
# 1. 전사 저장소 (STT 전송 규격 처리)
# --------------------------------------------------------------------------------------
class TranscriptStore:
    """
    규격 문서의 규칙
      - 줄 순서는 start_ms 기준 (id는 식별용). recovered 줄은 제자리에 끼워 넣는다.
      - edits: 같은 id는 rev가 가장 큰 것만 반영, deleted면 줄을 뺀다.
      - 사람이 고친 줄은 text_marked = text, low_confidence = False.
      - partial(미확정분)은 쓰지 않는다 (제안).
    """

    def __init__(self, meeting_id):
        self.meeting_id = meeting_id
        self.lines = {}
        self.rev = {}
        self.deleted = set()
        self.pending = {}                 # 아직 안 온 줄에 대한 수정 (줄이 오면 적용)

    def apply_batch(self, batch):
        for ln in batch.get("lines", []):
            i = ln["id"]
            if i in self.deleted:
                continue
            self.lines[i] = dict(ln)
            self.rev.setdefault(i, 0)
            pe = self.pending.pop(i, None)    # 줄보다 수정이 먼저 왔던 경우
            if pe and pe["rev"] > self.rev[i]:
                self.rev[i] = pe["rev"]
                self.lines[i]["text"] = pe["text"]
                self.lines[i]["text_marked"] = pe.get("text_marked", pe["text"])
                self.lines[i]["low_confidence"] = False
        for e in batch.get("edits", []):
            i = e["id"]
            if not e.get("deleted") and "text" not in e:
                continue                      # 글자는 안 바뀐 edit(예: 화자 표시만 바뀜)은 무시
            if not e.get("deleted") and i not in self.lines and i not in self.deleted:
                if e["rev"] > self.pending.get(i, {}).get("rev", 0):
                    self.pending[i] = dict(e)  # 줄이 아직 안 왔다: 보관
                continue
            if e["rev"] <= self.rev.get(i, 0):
                continue
            self.rev[i] = e["rev"]
            if e.get("deleted"):
                self.lines.pop(i, None)
                self.deleted.add(i)
            elif i in self.lines:
                self.lines[i]["text"] = e["text"]
                self.lines[i]["text_marked"] = e.get("text_marked", e["text"])
                self.lines[i]["low_confidence"] = False

    def snapshot(self):
        return sorted(self.lines.values(), key=lambda l: (l["start_ms"], l["end_ms"], l["id"]))


# --------------------------------------------------------------------------------------
# 2. 전사 파일 읽기 (jsonl -> 전송 규격의 줄)
# --------------------------------------------------------------------------------------
def marked_ratio(text_marked):
    """⟨ ⟩ 안의 글자 수 / 전체 글자 수 (공백 제외). jsonl은 글자 단위로 표시돼 있다."""
    inside = sum(len(m) for m in re.findall(r"⟨(.*?)⟩", text_marked))
    plain = re.sub(r"[⟨⟩]", "", text_marked)
    total = len(re.sub(r"\s", "", plain))
    return inside / total if total else 0.0


def load_jsonl_as_protocol_lines(path, low_conf_threshold=0.20):
    rows = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            if raw.strip():
                d = json.loads(raw)
                if "_meta" not in d:
                    rows.append(d)
    items = []
    for d in rows:
        start_ms, end_ms = round(d["start"] * 1000), round(d["end"] * 1000)
        arrival = end_ms + (RECOVERED_DELAY_MS if d["type"] == "recovered" else 0)
        items.append((arrival, start_ms, end_ms, d))
    items.sort(key=lambda x: (x[0], x[1]))
    out = []
    for n, (arrival, start_ms, end_ms, d) in enumerate(items, 1):
        out.append({
            "id": n,                                  # 실제 규격처럼 도착 순서로 번호를 붙임
            "arrival_ms": arrival,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "type": d["type"],
            "kind": d["kind"],
            "low_confidence": marked_ratio(d["text_marked"]) >= low_conf_threshold,
            "text": d["text"],
            "text_marked": d["text_marked"],
        })
    return out


def make_batches(lines, period_ms=20000):
    """도착 시각 기준으로 20초마다 묶음을 만든다 (전송 규격의 seq / sent_ms 흉내)."""
    last = max(l["arrival_ms"] for l in lines)
    batches, seq, prev = [], 0, -1
    t = period_ms
    while True:
        seq += 1
        chunk = [l for l in lines if prev < l["arrival_ms"] <= t]
        batches.append({"seq": seq, "sent_ms": t, "lines": chunk, "edits": [], "partial": None})
        prev = t
        if t >= last:
            break
        t += period_ms
    return batches


# --------------------------------------------------------------------------------------
# 3. 요약 입력 만들기
# --------------------------------------------------------------------------------------
def build_input(snapshot):
    """
    AI-06: kind가 backchannel/filler인 줄 제외 / AI-01: low_confidence 줄 제외.
    LLM에는 1부터 이어지는 번호를 준다 (id는 도착 순서라 시간 순서와 다르기 때문).
    """
    kept, dropped = [], []
    for ln in snapshot:
        if ln.get("kind") in ("backchannel", "filler"):
            dropped.append((ln, "kind"))
        elif ln.get("low_confidence"):
            dropped.append((ln, "low_confidence"))
        else:
            kept.append(ln)
    text = "\n".join(f"[{k}] {mmss(l['start_ms'])} {l['text_marked']}" for k, l in enumerate(kept, 1))
    return kept, dropped, text


SYSTEM_PROMPT = """너는 회의 전사에서 요약 항목을 뽑는 도구야. 입력은 STT가 만든 전사 줄이고, 줄마다 [번호 mm:ss]가 붙어 있어.

규칙
1. 입력에 있는 내용만 써. 입력에 없는 사실, 이름, 숫자, 이유를 만들지 마. 근거가 없으면 항목을 만들지 마.
2. 항목마다 근거 줄 번호 구간 evidence=[[첫 번호, 끝 번호], ...]를 달아. 시간은 쓰지 마(코드가 계산해). 구간의 양 끝은 입력에 있는 번호여야 해.
3. 항목 text는 한 줄로, "~함", "~임", "~나옴" 같은 명사형으로 끝내.
4. 전사는 STT 원본이라 오류가 있어. ⟨ ⟩로 묶인 글자는 STT가 확신하지 못한 부분이야. 그 부분을 근거로 쓰면 text에 "STT", "불확실", "불명확" 중 하나를 써서 불확실하다고 밝혀. 뜻을 네가 짐작해 고쳐 쓰지 마.
5. 말을 그대로 인용할 때는 「」로 묶고 입력 원문과 똑같이 써(⟨ ⟩ 기호는 빼).
6. 숫자(금액, 날짜, 시간)는 말한 한글 표기를 「」로 그대로 인용하고, 숫자로 바꾼 값은 "(250,000원으로 읽힘)"처럼 따로 적어.
7. 사람 이름과 거주지 같은 개인 정보는 쓰지 마. 군 경력, 학적, 가족 같은 개인 신상은 화제를 알아볼 만큼만 한 구절로 쓰고 세부는 쓰지 마. 옳고 그름을 판단하지 마.
8. 안건과 무관한 잡담은 항목 하나로 묶고, 하위 주제별로 evidence 구간을 따로 달아.
9. 결정 사항과 할 일은 따로 뽑지 마. 논의 요약만 해. 항목은 시간 순서로 써. "~하기로 함", "~로 정함"처럼 결정으로 쓰지 말고, 제안이면 "~라는 제안이 나옴", 의견이면 "~라는 의견이 나옴"으로 써.
10. topic은 짧은 이름이거나 null이야.
11. 화제가 바뀔 때마다 항목을 새로 만들어. 여러 화제를 한 항목에 뭉치지 마. 사용자 메시지에 적힌 항목 개수 범위를 지켜.
12. 한 항목의 근거 구간 하나는 길어야 30줄이고, 한 항목이 묶는 근거는 전부 합쳐 45줄을 넘기지 마. 잡담 구간도 하나로 뭉치지 말고 화제별로 나눠. 더 길면 화제별로 나눠서 항목을 따로 만들어.
13. text에는 누가 아니라 "무슨 말이 나왔는지"를 담아. 이유, 수량, 날짜, 질문과 그 답이 나오면 같이 써. "~에 대해 이야기를 나눔", "~에 대해 논의함"처럼 주제만 적지 마. 한 항목은 1~2문장이야.
14. STT가 틀린 것 같은 단어를 네가 맞는 단어로 고쳐 쓰지 마(예: 원문이 "주택"이면 "스펙"으로 바꾸지 마). 일번, 이번, 삼번 같은 말도 숫자로 바꿔 쓰지 말고, 뜻이 분명하지 않으면 「」로 원문 그대로 인용하고 "STT 불확실"이라고 밝혀.
15. 의미 있는 내용이 있는 줄은 어느 항목의 근거에든 들어가게 해. 인사나 짧은 호응처럼 내용이 없는 줄만 빼도 돼. 회의 끝부분의 제안(예: "쉬었다가 할까요")이나 일정 이야기는 인사가 아니니 꼭 항목으로 만들어.
16. 누가 만들었는지, 어떻게 처리했는지(결제 방법 등), 왜 그랬는지(이유)가 나오면 text에 꼭 넣어.
17. text에는 근거 줄에 없는 단어를 쓰지 마. 줄에 없는 사실, 자격증 이름, 직무명, 기술 용어를 덧붙이지 마.

출력은 JSON만. 형식:
{"summary": [{"text": "...", "topic": "...", "evidence": [[첫, 끝], [첫, 끝]]}]}"""


# --------------------------------------------------------------------------------------
# 4. LLM 호출
# --------------------------------------------------------------------------------------
USAGE_HOOK = None    # 서버가 지정한다: fn(provider, model, task, input_tokens, output_tokens). 토큰 사용량 지표용
OPERATION_HOOK = None  # 서버가 지정한다: fn(provider, model, task, duration_seconds, error_type)


def usage_of(provider, r):
    """모델 응답 객체에서 (입력 토큰, 출력 토큰). 알 수 없으면 (None, None)"""
    try:
        if provider == "gemini":
            u = r.usage_metadata
            return u.prompt_token_count, (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        if provider == "anthropic":
            return r.usage.input_tokens, r.usage.output_tokens
        if provider == "openai":
            return r.usage.prompt_tokens, r.usage.completion_tokens
    except AttributeError:
        pass
    return None, None


def report_usage(provider, model, task, r):
    if USAGE_HOOK is None:
        return
    tin, tout = usage_of(provider, r)
    if tin is None and tout is None:
        return
    try:
        USAGE_HOOK(provider, model, task, tin or 0, tout or 0)
    except Exception:
        pass                                # 지표 오류가 요약을 막으면 안 된다


def report_operation(provider, model, task, duration_seconds, error_type=None):
    if OPERATION_HOOK is None:
        return
    try:
        OPERATION_HOOK(provider, model, task, duration_seconds, error_type)
    except Exception:
        pass                                # 지표 오류가 요약을 막으면 안 된다


def _invoke_llm(provider, model, task, invoke):
    """실제 SDK 호출을 실행하고 성공·실패 시간을 기록한다. 실패 예외는 호출자에게 그대로 전달한다."""
    started = time.perf_counter()
    try:
        response = invoke()
    except Exception as exc:
        report_operation(provider, model, task, time.perf_counter() - started, type(exc).__name__)
        raise
    report_operation(provider, model, task, time.perf_counter() - started)
    report_usage(provider, model, task, response)
    return response


def call_llm(provider, model, system, user, mock_file=None, task=None):
    """task: 작업 종류. 토큰 사용량과 호출 시간 지표의 ai.task 속성으로 쓴다. live_summary(회의 중 요약),
    draft(끝난 회의 요약), agenda_classification(안건 분류)"""
    if provider == "mock":
        path = mock_file or os.path.join(HERE, "mock_llm_output.json")
        with open(path, encoding="utf-8") as f:
            return f.read()
    if provider == "gemini":
        from google import genai
        from google.genai import types
        client = genai.Client()   # GEMINI_API_KEY 환경변수

        def invoke():
            return client.models.generate_content(
                model=model, contents=user,
                config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json"))

        def response_text(r):
            return r.text
    elif provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic()   # ANTHROPIC_API_KEY 환경변수

        def invoke():
            return client.messages.create(model=model, max_tokens=8000, system=system,
                                          messages=[{"role": "user", "content": user}])

        def response_text(r):
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI()   # OPENAI_API_KEY 환경변수

        def invoke():
            return client.chat.completions.create(
                model=model, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])

        def response_text(r):
            return r.choices[0].message.content
    else:
        raise ValueError(f"알 수 없는 provider: {provider}")

    response = _invoke_llm(provider, model, task, invoke)
    return response_text(response)

def parse_llm_json(raw):
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("JSON을 찾지 못함")
    return json.loads(s[a:b + 1])


# --------------------------------------------------------------------------------------
# 5. 검증
# --------------------------------------------------------------------------------------
def build_char_stream(lines):
    """줄들을 이어 붙인 글자열과, 글자마다 'STT 불확실 표시 안에 있었는지' 마스크."""
    chars, mask = [], []
    for ln in lines:
        inside = False
        for ch in ln["text_marked"]:
            if ch == "⟨":
                inside = True
            elif ch == "⟩":
                inside = False
            elif not re.match(r"[\s\.\?\!,]", ch):
                chars.append(ch)
                mask.append(inside)
    return "".join(chars), mask



# 요약문에서 흔히 쓰는 말은 근거에 없어도 괜찮으니 검사에서 뺀다
COMMON_WORDS = ("의견 이야기 나옴 언급 논의 확인 제안 설명 가능성 경험 희망 고려 계획 상황 관련 분야 방식 과정 내용 정도 "
                "불확실 불명확 읽힘 읽힌 이라는 라는 하며 하고 에서 대해 대한 위해 통해 있는 있다 없는 있음 없음 같은 다른 여러 "
                "먼저 이후 이전 현재 당장 우선 가장 때문 경우 함께 또한 하는 되는 위한 등의 하기 하여 해서 하면 하자 한다 했다 했던 "
                "에게 으로 부터 보다 처럼 나눔 나눈 있어 있으며 없어 없으며 같이 모두 서로 각자 어떤 어떻게 무엇 다음 대화 질문 답변 "
                "여부 필요 중요 문제 방법 이유 결과 사람 참가자 시간 일정 이번 지금 나중 앞으로 아직 이미 계속 조금 많이 매우 "
                "정리 공유 전달 진행 준비 시작 종료 마무리 제기 표현 생각 판단 평가 비교 차이 관심 선호 요청 필요성 사용 발언 반응 취지 메시지 구간 부담 기준 현황 규모 능력 역량").split()
NOUN_JOSA = ("에서는", "으로는", "에서", "으로", "에게", "에는", "에도", "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만")
VERBISH = "다하되한된할될던나왔했겠며면고서지야자게든니라"
_OLD_JOSA = ("에서는", "으로는", "에서", "으로", "에게", "까지", "부터", "보다", "처럼", "이나", "이며", "하며", "에는", "에도", "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만", "며", "고")


def unsupported_words(text, source_stream):
    """요약문(인용 제외) 명사 후보 중 근거 줄에 글자 그대로 없는 것. 지어낸 사실을 사람이 확인하도록 돕는 후보 목록"""
    outside = re.sub(r"「.*?」", " ", text)
    outside = re.sub(r"\([^)]*\)", " ", outside)
    found = []
    for tok in re.findall(r"[가-힣]{3,}", outside):
        stem = None
        for j in NOUN_JOSA:                       # 명사+조사 모양인 것만 본다(동사/형용사 활용형은 제외)
            if tok.endswith(j) and len(tok) - len(j) >= 2:
                stem = tok[: -len(j)]
                break
        if stem is None or stem[-1] in VERBISH or len(stem) < 2:
            continue
        if any(stem.startswith(w) or w.startswith(stem) for w in COMMON_WORDS):
            continue
        if stem not in source_stream and stem not in found:
            found.append(stem)
    return found


LATIN_OK = {"ai", "stt", "ok", "jira", "git"}


def unknown_terms(text, full_stream):
    """요약문(인용 제외)에서 '전사 전체에도 없는' 단어 후보. 지어낸 고유명사나 STT 말을 몰래 고친 단어를 찾는 용도"""
    outside = re.sub(r"「.*?」", " ", text)
    outside = re.sub(r"\([^)]*\)", " ", outside)
    found = []
    for tok in re.findall(r"[가-힣]{3,}|[A-Za-z][A-Za-z0-9]+", outside):
        if re.match(r"[A-Za-z]", tok):
            cand = tok.lower()
            if cand in LATIN_OK:
                continue
        else:
            cand = None
            for j in NOUN_JOSA:
                if tok.endswith(j) and len(tok) - len(j) >= 2:
                    cand = tok[: -len(j)]
                    break
            if cand is None:
                cand = tok
            if cand[-1] in VERBISH or cand[-1] in "음임함됨짐옴듯" or len(cand) < 2:
                continue
            if any(cand.startswith(w) or w.startswith(cand) for w in COMMON_WORDS):
                continue
        if cand not in full_stream and cand not in found:
            found.append(cand)
    return found


VERIFY_PROMPT = """너는 회의 요약 검수자야. 항목마다 '요약'과 '근거 줄'을 줄게. 요약의 사실 주장이 근거 줄에 실제로 있는지만 판단해.

규칙
1. 근거 줄의 STT 오류는 그대로 인정하고, 뜻이 같게 쉬운 말로 풀어 쓴 것은 근거에 있는 것으로 봐.
2. 근거 줄에 없는 사실이 요약에 있으면 unsupported에 요약문 속 표현을 그대로 적어. 사실에는 이름, 회사, 숫자, 이유, 원인과 결과, 직무, 전공, 자격증, 기술 용어가 들어가.
3. 근거 줄이 서로 다른 사람의 말인데 한 사람이 말한 것처럼 섞었으면 unsupported야.
4. STT가 틀린 말을 요약이 맞는 말로 고쳐 썼는데 「」 인용이나 'STT 불확실' 표시가 없으면 unsupported야.
5. 요약에 숫자가 있으면 근거 줄 숫자와 한 자리까지 같은지 봐.
6. 근거에서 찾으면 ok=true, 찾지 못하면 ok=false야. note에는 왜 근거와 다른지 한 줄로 써.

출력은 JSON만. 형식:
{"checks": [{"item": 항목번호, "ok": true, "unsupported": [], "note": ""}]}"""


def verify_items(provider, model, results, kept, mock_file=None):
    """요약 항목을 근거 줄과 다시 대조하는 2차 호출. 반환: (문제 문장 리스트, 원문 응답)"""
    parts = []
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        if errs:
            continue
        ev = "\n".join(f"[{k}] {mmss(kept[k - 1]['start_ms'])} {kept[k - 1]['text_marked']}"
                       for a, b in spans for k in range(a, b + 1))
        parts.append(f"[항목 {n}]\n요약: {it.get('text')}\n근거 줄:\n{ev}")
    if not parts:
        return [], ""
    raw = call_llm(provider, model, VERIFY_PROMPT, "\n\n".join(parts), mock_file, task="draft")
    issues = []
    for c in parse_llm_json(raw).get("checks", []):
        bad = c.get("unsupported") or []
        if c.get("ok") is False or bad:
            issues.append(f"항목 {c.get('item')} 검수: 근거에 없거나 근거와 다른 내용 -> {', '.join(map(str, bad)) or '(표현 미지정)'}"
                          f" ({c.get('note', '')}) -> 빼거나 근거에 있는 말로 고쳐야 함")
    return issues, raw


def validate_items(items, kept):
    """반환: [(item, errors, warnings, spans)]"""
    n = len(kept)
    full_stream, _fm = build_char_stream(kept)
    results = []
    for it in items:
        errs, warns, spans = [], [], []
        text, topic, ev = it.get("text"), it.get("topic"), it.get("evidence")
        if not isinstance(text, str) or not text.strip():
            errs.append("text가 비어 있음")
            text = ""
        if topic is not None and not isinstance(topic, str):
            errs.append("topic은 문자열이나 null이어야 함")
        if not isinstance(ev, list) or not ev:
            errs.append("근거(evidence)가 없음 -> 근거 없는 항목은 저장하지 않음")
        else:
            for pair in ev:
                ok = (isinstance(pair, (list, tuple)) and len(pair) == 2
                      and all(isinstance(x, int) for x in pair) and 1 <= pair[0] <= pair[1] <= n)
                if ok:
                    spans.append((pair[0], pair[1]))
                else:
                    errs.append(f"근거 번호가 올바르지 않음: {pair} (유효 범위 1~{n})")
        if spans:
            pool = [kept[k - 1] for a, b in spans for k in range(a, b + 1)]
            stream, mask = build_char_stream(pool)
            quotes = re.findall(r"「(.*?)」", text)
            for q in quotes:
                qq = squash(q)
                pos = stream.find(qq) if qq else -1
                if pos < 0:
                    errs.append(f"인용이 근거 줄 원문에 없음: 「{q}」")
                elif kor_to_int(q) is not None and kor_to_int(q) >= 1000 and stream[pos + len(qq):pos + len(qq) + 1] and stream[pos + len(qq)] in "삼사오육칠팔구십백천만":
                    errs.append(f"숫자 인용이 원문보다 짧게 잘림(뒤에 '{stream[pos + len(qq)]}…'가 더 있음): 「{q}」")
                elif any(mask[pos:pos + len(qq)]) and not HEDGE.search(text):
                    errs.append(f"STT 불확실 글자가 든 인용인데 불확실 표시(STT/불확실/불명확)가 없음: 「{q}」")
                elif any(mask[pos:pos + len(qq)]):
                    warns.append(f"인용에 STT 불확실 글자 포함(본문에 불확실 표시 있음): 「{q}」")
            miss = unsupported_words(text, stream)
            if miss:
                warns.append("근거 줄에 없는 단어 후보(지어낸 사실인지 직접 확인): " + ", ".join(miss))
            gone = unknown_terms(text, full_stream)
            if gone:
                warns.append("전사 전체에도 없는 단어 후보(강한 의심, 원문에서 직접 확인): " + ", ".join(gone))
            # 숫자 검사: 4자리 이상 숫자는 한글 인용에서 계산한 값과 같아야 함
            outside = re.sub(r"「.*?」", "", text)
            for m in re.findall(r"\d[\d,]{2,}\d", outside):
                val = int(m.replace(",", ""))
                if val < 1000:
                    continue
                if not any(kor_to_int(q) == val for q in quotes):
                    errs.append(f"숫자 {m}에 해당하는 한글 인용이 없음(원문 한글 표기를 「」로 인용해야 함)")
        results.append((it, errs, warns, spans))
    return results


GENERIC_END = re.compile(r"(에 대해|에 관해|관련해?)[^.]*?(이야기를 나눔|이야기함|논의함|언급함|대화를 나눔)$")
DECISION_END = re.compile(r"(하기로 함|하기로 했음|하기로 정함|로 정함|로 결정함|결정함)$")
MAX_ITEM_LINES = 45     # 한 항목이 묶을 수 있는 근거 줄 수 합계 (잡담도 한 항목으로 뭉치지 않게)
MAX_SPAN_LINES = 30     # 근거 구간 하나의 최대 줄 수
MAX_GAP_LINES = 8       # 어느 항목에도 안 들어간 연속 줄이 이만큼 이상이면 경고


def item_range(kept):
    """줄 수에 맞는 권장 항목 개수 (최소, 최대)"""
    n = len(kept)
    lo = max(4, n // 12)
    return lo, max(lo + 3, n // 7)


def quality_issues(results, kept):
    """사실 검증과는 별개로, 요약이 너무 뭉뚱그려지거나 빠진 곳이 없는지 점검. 반환: 경고 문장 리스트"""
    ok = [r for r in results if not r[1]]
    lo, hi = item_range(kept)
    issues = []
    if len(ok) < lo:
        issues.append(f"항목이 너무 적음: {len(ok)}개 (권장 {lo}~{hi}개). 화제별로 더 잘게 나눠야 함")
    elif len(ok) > hi:
        issues.append(f"항목이 너무 많음: {len(ok)}개 (권장 {lo}~{hi}개). 비슷한 화제는 합쳐야 함")
    covered = set()
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        if errs:
            continue
        for a, b in spans:
            covered.update(range(a, b + 1))
            if b - a + 1 > MAX_SPAN_LINES:
                issues.append(f"항목 {n}의 근거 구간 [{a},{b}]가 {b - a + 1}줄로 너무 김 (최대 {MAX_SPAN_LINES}줄). 화제별로 항목을 나눠야 함")
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        if errs:
            continue
        tot = sum(b - a + 1 for a, b in spans)
        if tot > MAX_ITEM_LINES:
            issues.append(f"항목 {n}이 근거 {tot}줄을 한 항목으로 묶음 (최대 {MAX_ITEM_LINES}줄, 구간 {len(spans)}개). 화제별로 항목을 나눠야 함")
    k = 1
    N = len(kept)
    while k <= N:
        if k in covered:
            k += 1
            continue
        j = k
        while j + 1 <= N and (j + 1) not in covered:
            j += 1
        if j - k + 1 >= MAX_GAP_LINES:
            issues.append(f"줄 [{k},{j}] ({mmss(kept[k-1]['start_ms'])}~{mmss(kept[j-1]['end_ms'])}) {j - k + 1}줄이 어느 항목에도 들어가지 않음. 내용이 있으면 항목을 추가해야 함")
        k = j + 1
    # 주제만 적은 항목, 결정처럼 쓴 항목
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        if errs:
            continue
        t = (it.get("text") or "").strip().rstrip(".")
        if GENERIC_END.search(t) and len(t) < 90:
            issues.append(f"항목 {n}이 주제만 적음(무슨 말이 나왔는지 없음): {t[:50]}…")
        if DECISION_END.search(t):
            issues.append(f"항목 {n}이 결정처럼 쓰임(제안/의견으로 바꿔야 함): {t[:50]}…")
    full_stream, _m = build_char_stream(kept)
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        if errs:
            continue
        miss = [w for w in unknown_terms(it.get("text") or "", full_stream) if re.match(r"[a-z]", w) or len(w) >= 5]
        if miss:
            issues.append(f"항목 {n}에 전사 전체에도 없는 단어가 있음: {', '.join(miss)} -> 전사에 있는 말로 바꾸거나 빼야 함. STT가 틀린 말을 고쳐 쓴 거라면 「원문」(STT 불확실)로 인용해야 함")
    uncovered = [k for k in range(1, N + 1) if k not in covered]
    return issues, len(uncovered), uncovered


# --------------------------------------------------------------------------------------
# 6. 출력
# --------------------------------------------------------------------------------------
def spans_to_evidence(spans, kept):
    return [{"start": round(kept[a - 1]["start_ms"] / 1000, 2), "end": round(kept[b - 1]["end_ms"] / 1000, 2)}
            for a, b in spans]


def fmt_t(sec):
    return mmss(sec * 1000)


def write_outputs(out_dir, meeting_id, date, kept, dropped, input_text, raws, results, rejected):
    os.makedirs(out_dir, exist_ok=True)
    W = lambda name, s: open(os.path.join(out_dir, name), "w", encoding="utf-8").write(s)

    W("input_used.txt", input_text + "\n")
    W("dropped_lines.txt", "\n".join(
        f"[{mmss(l['start_ms'])}] ({why}) {l['text']}" for l, why in dropped) + "\n")
    for i, r in enumerate(raws, 1):
        W(f"llm_raw_{i}.txt", r)

    summary = []
    for it, errs, warns, spans in results:
        if errs:
            continue
        summary.append({"text": it["text"], "topic": it.get("topic"), "evidence": spans_to_evidence(spans, kept)})
    doc = {"schema_version": "0.1", "meeting_id": meeting_id, "date": date,
           "summary": summary, "decisions": [], "action_items": [], "extensions": None}
    W("summary.json", json.dumps(doc, ensure_ascii=False, indent=2) + "\n")

    # 사람이 보는 결과
    lines = [f"# 회의 요약 ({meeting_id})", "",
             f"- 입력 줄 {len(kept) + len(dropped)}개 중 사용 {len(kept)}개, 제외 {len(dropped)}개 "
             f"(low_confidence {sum(1 for _, w in dropped if w == 'low_confidence')}, "
             f"kind {sum(1 for _, w in dropped if w == 'kind')})",
             f"- 저장된 항목 {len(summary)}개, 검증 실패로 뺀 항목 {len(rejected)}개", ""]
    for n, (it, errs, warns, spans) in enumerate([r for r in results if not r[1]], 1):
        ev = ", ".join(f"{fmt_t(e['start'])}–{fmt_t(e['end'])}" for e in spans_to_evidence(spans, kept))
        lines.append(f"## {n}. [{it.get('topic') or '-'}] ({ev})")
        lines.append(it["text"])
        for w in warns:
            lines.append(f"- 주의: {w}")
        lines.append("")
        lines.append("근거 원문(구간마다 첫 줄과 끝 줄):")
        for a, b in spans:
            first, last = kept[a - 1], kept[b - 1]
            cut = lambda s: s if len(s) <= 70 else s[:70] + "…"
            lines.append(f"- [{mmss(first['start_ms'])}] {cut(first['text'])}")
            if b != a:
                lines.append(f"  … ({b - a + 1}줄) …")
                lines.append(f"- [{mmss(last['start_ms'])}] {cut(last['text'])}")
        lines.append("")
    W("report.md", "\n".join(lines))

    v = ["# 검증 결과", ""]
    for n, (it, errs, warns, spans) in enumerate(results, 1):
        status = "통과" if not errs else "실패(저장 안 함)"
        v.append(f"항목 {n} [{it.get('topic') or '-'}]: {status}")
        for e in errs:
            v.append(f"  - 오류: {e}")
        for w in warns:
            v.append(f"  - 주의: {w}")
    W("validation.txt", "\n".join(v) + "\n")
    return summary


# --------------------------------------------------------------------------------------
# 7. 실행
# --------------------------------------------------------------------------------------
def run(args):
    protocol_lines = load_jsonl_as_protocol_lines(args.input)

    # 전사 저장소로 받기: 한 번에 / 20초 묶음으로 흘려 보내기(--replay)
    store = TranscriptStore(args.meeting_id)
    batches = make_batches(protocol_lines) if args.replay else [{"lines": protocol_lines, "edits": []}]
    for b in batches:
        store.apply_batch(b)
    snapshot = store.snapshot()
    print(f"전사 줄 {len(snapshot)}개 ({len(batches)}번에 나눠 받음)")

    kept, dropped, input_text = build_input(snapshot)
    print(f"요약 입력 {len(kept)}줄 (제외 {len(dropped)}줄), 글자 {len(input_text)}자")

    lo, hi = item_range(kept)
    minutes = (kept[-1]["end_ms"] - kept[0]["start_ms"]) / 60000 if kept else 0
    user = (f"다음 전사를 요약해줘. 전사는 {len(kept)}줄, 약 {minutes:.0f}분 분량이야. "
            f"항목은 {lo}~{hi}개 범위로 만들어줘.\n\n" + input_text)
    raws, parsed = [], None
    vissues, vraws = [], []
    attempts = 1 if args.provider == "mock" else 1 + args.retries
    current_user = user
    for attempt in range(1, attempts + 1):
        raw = call_llm(args.provider, args.model, SYSTEM_PROMPT, current_user, args.mock_file, task="draft")
        raws.append(raw)
        try:
            parsed = parse_llm_json(raw)
            items = parsed["summary"]
        except Exception as e:
            print(f"시도 {attempt}: JSON 해석 실패: {e}")
            current_user = user + f"\n\n[이전 출력]\n{raw}\n\n[오류]\nJSON 해석 실패({e}). 형식에 맞게 JSON만 다시 출력해."
            items = []
            continue
        results = validate_items(items, kept)
        bad = [(i, r) for i, r in enumerate(results, 1) if r[1]]
        quality, _unc, _ul = quality_issues(results, kept)
        vissues = []
        if args.verify:
            try:
                vissues, vraw = verify_items(args.provider, args.model, results, kept, args.mock_verify_file)
                vraws.append(vraw)
            except Exception as e:
                print(f"시도 {attempt}: 검수 호출 실패(건너뜀): {e}")
            quality = quality + vissues
        print(f"시도 {attempt}: 항목 {len(items)}개, 검증 실패 {len(bad)}개, 품질 경고 {len(quality)}개"
              + (f" (검수 지적 {len(vissues)}개 포함)" if args.verify else ""))
        if not bad and not quality:
            break
        msg = "\n".join(f"- 항목 {i}: " + " / ".join(r[1]) for i, r in bad)
        qmsg = "\n".join(f"- {q}" for q in quality)
        current_user = (user + f"\n\n[이전 출력]\n{raw}\n\n[검증 오류]\n{msg or '없음'}\n\n[품질 경고]\n{qmsg or '없음'}\n"
                        "오류가 있는 항목은 고치고, 품질 경고를 해결해서 전체 JSON을 다시 출력해.")
    if parsed is None or not items:
        print("요약을 얻지 못했습니다. llm_raw 파일을 확인하세요.")
        results = []
    rejected = [r for r in results if r[1]]
    quality, unc, ulist = quality_issues(results, kept) if results else (["요약 결과 없음"], len(kept), list(range(1, len(kept) + 1)))
    quality = quality + vissues
    os.makedirs(args.out, exist_ok=True)
    for i, vr in enumerate(vraws, 1):
        with open(os.path.join(args.out, f"verify_raw_{i}.txt"), "w", encoding="utf-8") as f:
            f.write(vr)
    with open(os.path.join(args.out, "quality.txt"), "w", encoding="utf-8") as f:
        f.write(f"# 품질 점검\n\n근거에 포함된 줄: {len(kept) - unc}/{len(kept)}줄 (빠진 줄 {unc}개)\n")
        f.write("\n".join(f"- {q}" for q in quality) if quality else "경고 없음")
        f.write("\n")
        if ulist:
            f.write("\n## 어느 항목에도 안 들어간 줄\n")
            for k in ulist:
                f.write(f"[{k}] {mmss(kept[k-1]['start_ms'])} {kept[k-1]['text']}\n")
    summary = write_outputs(args.out, args.meeting_id, args.date, kept, dropped, input_text, raws, results, rejected)

    print(f"\n저장된 항목 {len(summary)}개, 검증 실패로 뺀 항목 {len(rejected)}개")
    print(f"근거에 포함된 줄 {len(kept) - unc}/{len(kept)}, 품질 경고 {len(quality)}개 (quality.txt 참고)")
    for q in quality:
        print(f"  ! {q}")
    for n, s in enumerate(summary, 1):
        ev = ", ".join(f"{fmt_t(e['start'])}–{fmt_t(e['end'])}" for e in s["evidence"][:2])
        more = " …" if len(s["evidence"]) > 2 else ""
        print(f" {n:2d}. [{s['topic'] or '-'}] {s['text'][:60]}… ({ev}{more})")
    if rejected:
        print("\n검증 실패 항목:")
        for it, errs, _, _ in rejected:
            print(f" - [{it.get('topic') or '-'}] {str(it.get('text'))[:40]}…")
            for e in errs:
                print(f"     오류: {e}")
    print(f"\n결과 폴더: {os.path.abspath(args.out)}  (report.md를 열어 보세요)")
    return summary, snapshot, input_text


def selftest():
    # 1) 전송 규격: rev가 큰 것만 반영, deleted는 제거, 늦게 온 recovered는 제자리에 정렬
    st = TranscriptStore("t")
    st.apply_batch({"lines": [
        {"id": 12, "start_ms": 10600, "end_ms": 19400, "type": "final", "kind": None, "low_confidence": True,
         "text": "a", "text_marked": "⟨a⟩"},
        {"id": 13, "start_ms": 9800, "end_ms": 10400, "type": "recovered", "kind": "backchannel",
         "low_confidence": False, "text": "b", "text_marked": "b"}], "edits": []})
    assert [l["id"] for l in st.snapshot()] == [13, 12]
    st.apply_batch({"lines": [], "edits": [{"id": 12, "rev": 2, "text": "고침", "text_marked": "고침"},
                                           {"id": 12, "rev": 1, "text": "옛날", "text_marked": "옛날"}]})
    l12 = [l for l in st.snapshot() if l["id"] == 12][0]
    assert l12["text"] == "고침" and l12["low_confidence"] is False
    st.apply_batch({"lines": [], "edits": [{"id": 13, "rev": 1, "deleted": True}]})
    assert [l["id"] for l in st.snapshot()] == [12]
    # 1-2) 줄보다 수정이 먼저 오면 보관했다가, 줄이 올 때 적용 (높은 rev만)
    st.apply_batch({"lines": [], "edits": [{"id": 20, "rev": 1, "text": "옛 수정"}, {"id": 20, "rev": 2, "text": "새 수정"}]})
    assert 20 not in [l["id"] for l in st.snapshot()]
    st.apply_batch({"lines": [{"id": 20, "start_ms": 30000, "end_ms": 31000, "type": "final", "kind": None,
                               "low_confidence": True, "text": "원본", "text_marked": "⟨원본⟩"}], "edits": []})
    l20 = [l for l in st.snapshot() if l["id"] == 20][0]
    assert l20["text"] == "새 수정" and l20["low_confidence"] is False and 20 not in st.pending
    # 2) 한글 숫자
    assert kor_to_int("삼십 오만 칠천 삼백 오십 팔") == 357358
    assert kor_to_int("만 원") == 10000 and kor_to_int("이백 명") == 200
    # 3) 검증기가 잡는 오류
    kept = [{"start_ms": 0, "end_ms": 1000, "text": "삼십 오만 칠천 삼백 오십 팔", "text_marked": "삼십 ⟨오⟩만 칠천 삼백 오십 팔"}]
    good = {"text": "금액은 「삼십 오만 칠천 삼백 오십 팔」(357,358원으로 읽힘, 일부 글자 STT 불확실)", "topic": None, "evidence": [[1, 1]]}
    bad1 = {"text": "금액은 「삼십 오만 칠천 삼백 오십 팔」(357,358원으로 읽힘)", "topic": None, "evidence": [[1, 1]]}
    bad2 = {"text": "금액은 「사십만」", "topic": None, "evidence": [[1, 1]]}
    bad3 = {"text": "내용", "topic": None, "evidence": [[1, 5]]}
    bad4 = {"text": "금액은 357,358원", "topic": None, "evidence": [[1, 1]]}
    r = validate_items([good, bad1, bad2, bad3, bad4], kept)
    assert [bool(x[1]) for x in r] == [False, True, True, True, True], r
    # 3-2) 숫자 인용이 중간에 잘린 경우(예: 마지막 '팔'을 빼고 357,350으로 읽는 실수)
    trunc = {"text": "금액은 「삼십 오만 칠천 삼백 오십」(357,350원으로 읽힘, 일부 글자 STT 불확실)", "topic": None, "evidence": [[1, 1]]}
    assert validate_items([trunc], kept)[0][1], "잘린 숫자 인용을 못 잡음"
    # 4) 검수 응답 해석
    fake = '{"checks":[{"item":2,"ok":false,"unsupported":["수출 호황"],"note":"근거에 없음"},{"item":1,"ok":true,"unsupported":[],"note":""}]}'
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        tf.write(fake)
    got, _ = verify_items("mock", None, validate_items([good, good], kept), kept, tf.name)
    os.unlink(tf.name)
    assert len(got) == 1 and "수출 호황" in got[0], got
    # 5) 토큰 사용량 읽기와 보고
    from types import SimpleNamespace as NS
    assert usage_of("gemini", NS(usage_metadata=NS(prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5))) == (100, 25)
    assert usage_of("anthropic", NS(usage=NS(input_tokens=7, output_tokens=3))) == (7, 3)
    assert usage_of("openai", NS(usage=NS(prompt_tokens=9, completion_tokens=4))) == (9, 4)
    assert usage_of("gemini", NS()) == (None, None)
    global USAGE_HOOK
    got = []
    USAGE_HOOK = lambda *a: got.append(a)
    try:
        report_usage("openai", "m", "live_summary", NS(usage=NS(prompt_tokens=9, completion_tokens=4)))
        report_usage("openai", "m", "live_summary", NS())                  # 사용량이 없으면 보고하지 않음
        USAGE_HOOK = lambda *a: 1 / 0
        report_usage("openai", "m", "t", NS(usage=NS(prompt_tokens=1, completion_tokens=1)))   # 지표 오류는 삼킴
    finally:
        USAGE_HOOK = None
    assert got == [("openai", "m", "live_summary", 9, 4)], got
    print("자체 시험 통과 (전송 규격 처리, 한글 숫자 변환, 검증기, 토큰 사용량)")


def load_dotenv(path):
    """저장소 루트의 .env 에서 아직 설정되지 않은 환경변수만 읽어 온다 (값은 출력하지 않는다)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(os.path.join(HERE, "..", ".env"))   # API 키를 매번 입력하지 않도록
    ap = argparse.ArgumentParser(description="끝난 회의 요약")
    ap.add_argument("--input", default="meeting1_full_transcript.jsonl", help="STT 전사 jsonl")
    ap.add_argument("--out", default="out", help="결과 폴더")
    ap.add_argument("--provider", default="mock", choices=["mock", "gemini", "anthropic", "openai"])
    ap.add_argument("--model", default=None, help="모델 ID (anthropic은 생략하면 claude-sonnet-5-5)")
    ap.add_argument("--mock-file", default=None, help="mock 응답 파일 (기본: mock_llm_output.json)")
    ap.add_argument("--meeting-id", default="m_sample")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--retries", type=int, default=1, help="검증 실패 시 다시 요청하는 횟수 (mock은 안 함)")
    ap.add_argument("--verify", action="store_true", help="요약을 만든 뒤 모델을 한 번 더 불러 근거 줄과 대조(호출 횟수 2배)")
    ap.add_argument("--mock-verify-file", default=os.path.join(HERE, "mock_verify_output.json"), help="mock 검수 응답 파일")
    ap.add_argument("--replay", action="store_true", help="전사를 20초 묶음으로 나눠 받는 것처럼 흘려 넣기")
    ap.add_argument("--selftest", action="store_true", help="자체 시험만 실행")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.provider == "anthropic" and not args.model:
        args.model = "claude-sonnet-5-5"
    if args.provider in ("gemini", "openai") and not args.model:
        ap.error(f"--provider {args.provider}는 --model(모델 ID)이 필요합니다")
    run(args)


if __name__ == "__main__":
    main()
