#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
주제 변경 감지 평가 (AI-06)

끝난 회의 전사를 실제 서비스처럼 10초 묶음으로 흘려 보내며, 사람이 확정한 분류(labels_confirmed.tsv)를 그대로 써서 감지가 언제 몇 번 나오는지 본다.
분류 모델은 부르지 않는다. 감지는 분류 결과만 읽으므로 모델 없이 되고, 모델을 부르는 것은 새 안건 제목을 만들 때뿐이다.

두 가지로 쓴다
  1) 감지 점검 (기본, 비용 0): 해당 없음이 끊김 없이 이어진 가장 긴 구간, 제안(SUGGEST)이 나온 횟수와 시각을 낸다.
     --expect-none 이면 "제안이 한 번도 나오지 않아야 한다"를 기준으로 통과 여부를 낸다 (오경보 확인).
  2) 제목 점검 (--titles, 유료): 제안이 나온 구간마다 모델을 불러 제목을 만들고 근거 검증을 통과하는지 본다.
     제목 품질은 점수가 아니라 사람이 읽고 판단한다. 제목은 화면에만 보여 주고 결과 파일에는 저장하지 않는다 (전사·모델 문장 금지).

결과 JSON 에는 전사와 제목을 넣지 않는다 (시각, 줄 번호, 개수만).

사용
  # 오경보 확인 (비용 0)
  python3 eval_agenda_offtopic.py --transcript T.jsonl --agendas A.json --labels L.tsv --expect-none
  # 호출 수와 비용 추정만 (비용 0)
  python3 eval_agenda_offtopic.py ... --titles --dry-run --provider gemini --model gemini-3.8-flash
  # 제목 점검 (한 번만. 결과 파일이 이미 있으면 거부) -- 키는 환경변수 또는 저장소 루트 .env 로만
  python3 eval_agenda_offtopic.py ... --titles --provider gemini --model gemini-3.8-flash --paid-ok --out R.json

정답 라벨은 사람이 확정한 것만 쓴다. 확정되지 않은 칸은 --use-draft 일 때만 초안으로 채우며 이때 결과는 점검용(최종 수치 아님)이다.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from meeting_summary import load_jsonl_as_protocol_lines, make_batches  # noqa: E402
from input_adapter import adapt_agendas  # noqa: E402
import agenda_classifier as ac  # noqa: E402
import agenda_offtopic as ao  # noqa: E402
import eval_agenda as ea  # noqa: E402
import eval_agenda_summary as es  # noqa: E402

CRITERIA = {                        # 제안 기준. 실행 전에 확인받는다
    "falseAlarms": 0,               # --expect-none 인 회의에서 제안이 나온 횟수
    "titleValid": 1.0,              # 제안이 나온 구간 중 검증을 통과한 제목이 나온 비율 (지어낸 내용은 사람이 읽고 따로 본다)
}
OUT_TOK_PER_CALL = 40               # 호출 한 번의 출력 (제목 한 줄과 근거) 추정
MAX_CALLS_PER_EPISODE = 4   # 제목 2회(재시도 포함) + 긴 구간 최근 줄 재시도 2회


class TitleRecorder:
    """제목 모델 호출을 감싸 호출 수와 입력 글자 수를 센다. dry 이면 모델을 부르지 않고 에코 답을 쓴다."""

    def __init__(self, inner, dry):
        self.inner, self.dry = inner, dry
        self.calls, self.in_chars = 0, 0

    def __call__(self, user):
        self.calls += 1
        self.in_chars += len(ao.SYSTEM_PROMPT) + len(user)
        return ao.echo_llm(user) if self.dry else self.inner(user)


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


def replay(batches, agendas_msg, gold, provider, model, dry, use_model, cfg_kw):
    """묶음을 흘려 보내며 제안이 나오는 시점을 모은다. 반환: (결과, 제목 기록기, 제목 목록(화면 전용))."""
    ad = adapt_agendas(agendas_msg)
    ccfg = ac.ClassifyCfg(provider="echo")
    cls = ac.AgendaClassifier(ad["meeting_id"], ccfg)
    cls.set_agendas(ad["version"], ad["agendas"])
    cls.apply_corrections([{"seq": s, "agenda_id": a, "rev": 1} for s, a in gold.items()])
    cls.halted = "평가: 사람이 확정한 분류만 사용"                 # 분류 모델은 부르지 않는다
    ocfg = ao.OffTopicCfg(provider=provider if use_model else "echo", model=model, **cfg_kw)
    rec = TitleRecorder(ao.make_llm(ocfg) if use_model else ao.echo_llm, dry or not use_model)
    off = ao.AgendaOffTopic(cls, ocfg, llm=rec)
    episodes, screen_titles, max_run, last_stage, cur = [], [], 0, None, None
    for b in batches:
        cls.receive(b)
        cls.advance(b["sent_ms"])
        off.process()
        run = off._trailing_run(off._view())
        if run:
            max_run = max(max_run, run["end"] - run["start"])
        bd = off.board()
        stage = bd["stage"] if bd else None
        if bd and (cur is None or cur["detectedAtMs"] != bd["detectedAtMs"]):
            cur = {"detectedAtMs": bd["detectedAtMs"], "firstStage": stage, "stages": [stage]}
            episodes.append(cur)
            screen_titles.append(bd)
        elif bd and cur is not None:
            if stage != cur["stages"][-1]:
                cur["stages"].append(stage)
            screen_titles[-1] = bd
        if bd:
            cur["titleOk"] = bd["suggestedTitle"] is not None
            cur["evidenceSeqs"] = list(bd["evidenceSeqs"])
        last_stage = stage
    off.process(final=True)
    return {"maxNoneRunSec": round(max_run / 1000, 1), "suggestions": off.info()["suggested"], "episodes": episodes,
            "info": off.info(), "endStage": last_stage}, rec, screen_titles


def judge_pass(res, expect_none, titles):
    r = {}
    if expect_none:
        r["falseAlarms"] = res["suggestions"] <= CRITERIA["falseAlarms"]
    if titles and res["episodes"]:
        ok = sum(1 for e in res["episodes"] if e.get("titleOk"))
        r["titleValid"] = ok / len(res["episodes"]) >= CRITERIA["titleValid"]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--agendas", required=True)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--provider", default="echo")
    ap.add_argument("--model")
    ap.add_argument("--titles", action="store_true", help="제안이 나온 구간마다 모델을 불러 제목을 만든다 (유료)")
    ap.add_argument("--expect-none", action="store_true", help="이 회의에서는 제안이 한 번도 나오지 않아야 한다 (오경보 확인)")
    ap.add_argument("--threshold-sec", type=int, default=180)
    ap.add_argument("--dry-run", action="store_true", help="모델을 부르지 않고 호출 수와 비용 추정만 낸다")
    ap.add_argument("--paid-ok", action="store_true", help="유료 호출 허용 (기준과 비용을 확인한 뒤에만)")
    ap.add_argument("--use-draft", action="store_true", help="확정 전 라벨 초안을 쓴다 (점검용)")
    ap.add_argument("--out", help="결과 JSON 경로 (유료 실행은 이미 있으면 거부)")
    a = ap.parse_args()

    paid = a.titles and a.provider != "echo" and not a.dry_run
    if paid and not a.paid_ok:
        sys.exit("유료 호출입니다. 기준과 비용을 확인한 뒤 --paid-ok 를 붙이세요. (--dry-run 으로 추정만 볼 수 있습니다)")
    if paid and not a.model:
        sys.exit("--model 이 필요합니다")
    if paid:
        load_dotenv(os.path.join(HERE, "..", ".env"))
        if a.provider == "gemini" and not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
            sys.exit("GEMINI_API_KEY 를 찾지 못했습니다 (환경변수와 저장소 루트 .env 모두 비어 있음). 호출하지 않고 끝냅니다")
    if paid and (not a.out or os.path.exists(a.out)):
        sys.exit("유료 실행은 --out 이 필요하고, 그 파일이 이미 있으면 다시 돌리지 않습니다 (한 번만)")

    lines = load_jsonl_as_protocol_lines(a.transcript)
    batches = make_batches(lines, period_ms=10000)
    with open(a.agendas, encoding="utf-8") as f:
        agendas_msg = json.load(f)
    gold, _unsure = ea.load_labels(a.labels, a.use_draft)
    use_model = a.titles and a.provider != "echo"
    dry = a.dry_run or not use_model
    res, rec, screen = replay(batches, agendas_msg, gold, a.provider, a.model, dry, use_model, {"threshold_sec": a.threshold_sec})
    out = {"provider": a.provider, "model": a.model, "dryRun": dry, "labels": len(gold), "labelsAreDraft": bool(a.use_draft),
           "thresholdSec": a.threshold_sec, "expectNone": a.expect_none, "criteria": CRITERIA, "result": res}
    if a.titles:
        cost = es.cost(rec.in_chars, rec.calls * OUT_TOK_PER_CALL, a.model)
        out["titleCalls"] = rec.calls
        out["cost"] = cost
        out["worstCaseCalls"] = res["suggestions"] * MAX_CALLS_PER_EPISODE
    if not a.dry_run:
        broken = res["info"]["halted"]
        if broken and use_model:
            out["invalid"] = f"모델 호출이 멈춰 점수를 내지 않음: {broken}"
        else:
            out["pass"] = judge_pass(res, a.expect_none, use_model)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    if use_model and not a.dry_run and screen:                # 제목은 화면에만 보여 준다 (파일에는 저장하지 않는다)
        print("\n[화면에만 표시 - 결과 파일에는 저장하지 않음] 제안 구간별 제목을 읽고 판단하세요 (전사에 없는 내용, 등록된 안건과 겹침, 짧은 명사구인지)")
        for k, bd in enumerate(screen, 1):
            print(f"  {k}. 제목={bd['suggestedTitle']!r} 근거 줄={bd['evidenceSeqs']} 단계={bd['stage']}")
    if "invalid" in out:
        print("\n[경고] 모델 호출이 멈춰 결과가 유효하지 않습니다. 원인을 고친 뒤 새 파일 이름으로 다시 돌려야 합니다.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
