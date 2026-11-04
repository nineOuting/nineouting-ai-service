"""STT 오인식 교정 시험 (명세 7.7). 합성 사례: fixtures/eval_stt_request.json"""
import json
import logging
from pathlib import Path

import pytest

from minutes.correction import (CORRECTION_KIND, agenda_text, apply, build_user, check_fix, correct, read_fixes,
                                similarity, source_text)
from minutes.draft import SUCCEEDED, make_draft
from minutes.materials import build_materials
from minutes.placement import load_prompt
from minutes_fakes import ScriptLLM, previous_lines_of
from shared.llm import LLMError, LLMTimeout

FIXTURE = Path(__file__).parent / "fixtures" / "eval_stt_request.json"
TITLES = ["안건별 논의", "결정 사항", "미결 안건", "할 일"]
RIGHT = {"fixes": [{"wrong": "쿠버내티스", "right": "쿠버네티스"}, {"wrong": "프로메테오스", "right": "프로메테우스"},
                   {"wrong": "그라파냐", "right": "그라파나"}]}


@pytest.fixture
def body():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def materials(body):
    return build_materials(body["agendas"], body["summary"])


def answer(materials):
    """모든 재료를 종류에 맞는 칸에 놓는 칸 고르기 답."""
    section = {"decision": 2, "undecided": 3, "todo": 4}
    return {"placements": [{"m": m.ref, "section": section.get(m.kind, 1)} for m in materials], "unplaced": []}


def by_ref(materials):
    return {m.ref: m for m in materials}


# ---- 쌍 검사

def ok(body, materials, wrong, right, reading=None, targets=None):
    targets = [m for m in materials if not m.edited] if targets is None else targets
    return check_fix(wrong, right, reading, targets, source_text(body["summary"], body["agendas"]),
                     agenda_text(body["agendas"]))


@pytest.mark.parametrize("wrong, right", [("쿠버내티스", "쿠버네티스"), ("프로메테오스", "프로메테우스"), ("그라파냐", "그라파나"),
                                         ("얼러트 매니져", "얼러트 매니저"),                   # 맞는 표기가 빠진 의견에만 있음
                                         ("깃헙 액선", "깃허브 액션")])                       # 4자 → 5자, 0.77
def test_맞는_표기가_요청에_있으면_받아들인다(materials, body, wrong, right):
    assert ok(body, materials, wrong, right)


@pytest.mark.parametrize("wrong, right, why", [
    ("젠킨수", "젠킨스", "맞는 표기가 요청에 없음"),
    ("핼름", "헬름", "맞는 표기가 「」 안에만 있음"),
    ("다섯 개", "여섯 개", "수 낱말"),
    ("쿠버내티스", "쿠버네티스 클러스터", "한쪽이 다른 쪽을 품음"),
    ("요고사항 문서", "요구사항", "글자 수가 다르고 비슷함이 0.6 아래 (0.57)"),
    ("쿠버", "쿠버네티스", "한쪽이 다른 쪽을 품음"),
    ("저장소", "프로메테우스", "글자 수가 3자 다름"),
    ("없는말", "그라파나", "잘못 적힌 말이 재료에 없음"),
])
def test_검사에_걸리는_쌍은_버린다(materials, body, wrong, right, why):
    assert not ok(body, materials, wrong, right), why


def test_영어_표기는_안건_글에_있고_읽는_소리가_비슷하면_받아들인다(materials, body):
    assert ok(body, materials, "알고 시디", "ArgoCD", "아르고 시디")                      # 0.80


@pytest.mark.parametrize("wrong, right, reading, why", [
    ("알고 시디", "ArgoCD", None, "읽는 소리가 없음"),
    ("알고 시디", "ArgoCD", "ArgoCD", "읽는 소리가 한글이 아님"),
    ("알고 시디", "ArgoCD", "예산", "읽는 소리가 비슷하지 않음"),
    ("젠킨수", "Jenkins", "젠킨스", "영어 표기가 요약 글에만 있고 안건 글에는 없음"),
    ("에이더블유에스", "AWS", "에이더블유에스", "맞게 읽은 소리를 영어로 바꾸는 것뿐 (오인식 아님)"),
    ("알고 시디", "ArgoCD 2", "아르고 시디 이", "숫자"),
])
def test_검사에_걸리는_영어_쌍은_버린다(materials, body, wrong, right, reading, why):
    assert not ok(body, materials, wrong, right, reading), why


def test_사람이_고친_재료에만_있는_말은_바꾸지_않는다(materials, body):
    edited = [m for m in materials if m.edited]
    assert edited and "쿠버내티스" in edited[0].text
    assert not ok(body, materials, "쿠버내티스", "쿠버네티스", targets=[])


def test_글자_수가_달라도_비슷함이_높으면_받아들인다():
    from minutes.models import Material
    m = Material(ref="m1", agenda_id=1, agenda_title="t", kind="summary", item_id="s", label="요약",
                 text="「기능면 시선」을 같이 봄", evidence_seqs=(), edited=False, needs_check=False, order=(0, 1, 0))
    assert check_fix("기능면 시선", "기능 명세", None, [m], "기능 명세 문서", "")            # 5자 → 4자, 0.62
    assert not check_fix("기능면 시선", "기능", None, [m], "기능", "")                     # 품음


def test_교정은_정해_둔_생각하는_양으로_부르고_칸_고르기는_넘기지_않는다(body, materials, monkeypatch):
    import minutes.correction as c
    llm = ScriptLLM(answer(materials), corrections=[RIGHT])
    make_draft(body, llm)
    assert llm.efforts == [None, None]                    # 지금은 모델 기본값
    monkeypatch.setattr(c, "EFFORT", "low")
    llm = ScriptLLM(answer(materials), corrections=[RIGHT])
    make_draft(body, llm)
    assert llm.efforts == ["low", None]


def test_교정_코드에서_예외가_나도_초안은_만든다(body, materials, monkeypatch):
    import minutes.correction as c
    monkeypatch.setattr(c, "apply", lambda *a: 1 / 0)
    result = make_draft(body, ScriptLLM(answer(materials), corrections=[RIGHT]))
    assert result["status"] == SUCCEEDED and all("STT:" not in s["content"] for s in result["sections"])


def test_소리가_다른_말은_비슷함이_낮다():
    assert similarity("테로폰", "테라폼") > similarity("태로폰", "테라폼") >= 0.5
    assert similarity("예산", "일정") < 0.5 and similarity("배포", "개발") < 0.5


def test_출처에는_인용_안의_글이_없다(materials, body):
    sources = source_text(body["summary"], body["agendas"])
    assert "헬름" not in sources and "쿠버네티스" in sources and "그라파나" in sources
    assert "ArgoCD" in agenda_text(body["agendas"]) and "Jenkins" not in agenda_text(body["agendas"])


def test_재료에서_빠진_의견도_출처이고_AI에_참고_글로_보낸다(materials, body):
    assert all("얼러트 매니저" not in m.text for m in materials)            # 요약 문장이 담아 재료가 되지 않음
    assert "얼러트 매니저" in source_text(body["summary"], body["agendas"])
    user = build_user(materials, body["agendas"], body["summary"])
    assert "<참고 글>\n- 얼러트 매니저 설정이 복잡하다는 우려\n</참고 글>" in user


# ---- 답 읽기

def test_답에서_모양이_맞는_쌍만_읽는다():
    pairs, problems = read_fixes({"fixes": [{"wrong": " 테로폰 ", "right": "테라폼", "reading": " "}, {"wrong": 1}, "x",
                                            {"wrong": "테로폰", "right": "테라폼"},
                                            {"wrong": "태로폰", "right": "Terraform", "reading": "테라폼 "}]})
    assert pairs == [("테로폰", "테라폼", None), ("태로폰", "Terraform", "테라폼")] and problems == []
    assert read_fixes({"placements": []})[0] is None


# ---- 반영

def test_인용_안이든_밖이든_같은_모양으로_바꾸고_원문을_남긴다(materials):
    fixed = by_ref(apply(materials, [("쿠버내티스", "쿠버네티스"), ("프로메테오스", "프로메테우스")]))
    assert fixed["m2"].text.startswith("「쿠버네티스(STT: 쿠버내티스)」 클러스터는")
    assert fixed["m11"].text.startswith("프로메테우스(STT: 프로메테오스) 보관 기간을")
    assert fixed["m7"].text == "쿠버네티스(STT: 쿠버내티스) 설정 파일 정리 (박민수, 금요일까지)"


def test_표_조각도_같이_바꾼다(materials):
    fixed = by_ref(apply(materials, [("쿠버내티스", "쿠버네티스")]))
    assert dict(fixed["m7"].parts)["할 일"] == "쿠버네티스(STT: 쿠버내티스) 설정 파일 정리"
    assert dict(fixed["m2"].parts)["문장"] == fixed["m2"].text


def test_사람이_고친_재료와_해당_없는_재료는_그대로다(materials):
    fixed = apply(materials, [("쿠버내티스", "쿠버네티스")])
    original = by_ref(materials)
    assert by_ref(fixed)["m5"] is original["m5"] and "STT" not in by_ref(fixed)["m5"].text     # 사람이 고친 의견
    assert by_ref(fixed)["m9"] is original["m9"]


def test_긴_말부터_한_번에_바꿔_겹쳐_바꾸지_않는다(materials):
    m = by_ref(materials)["m13"]
    fixed = apply([m], [("그라파냐", "그라파나"), ("그라파냐 대시보드", "그라파나 대시보드")])
    assert fixed[0].text.startswith("그라파나 대시보드(STT: 그라파냐 대시보드) 정리")


# ---- AI 호출

def test_AI에는_사람이_고친_재료를_보내지_않는다(materials, body):
    user = build_user(materials, body["agendas"], body["summary"])
    assert "m5 " not in user and "m2 「쿠버내티스」" in user
    assert "- 그라파나 모니터링" in user and "  결정할 사항: 쿠버네티스 배포 방식" in user


def test_교정은_통과한_쌍만_반영하고_개수를_돌려준다(materials, body):
    reply = {"fixes": RIGHT["fixes"] + [{"wrong": "젠킨수", "right": "젠킨스"}]}
    c = correct(materials, body["agendas"], body["summary"], ScriptLLM(corrections=[reply]), 60.0)
    assert (c.fixes, c.applied, c.changed, c.error_code, c.attempts) == (4, 3, 4, None, 1)     # m2·m7·m11·m13
    assert "젠킨수」" in by_ref(c.materials)["m3"].text


@pytest.mark.parametrize("reply, code", [(LLMTimeout("t"), "LLM_TIMEOUT"), (LLMError("e"), "LLM_FAILED"),
                                         ("모르겠음", "CHECK_FAILED"), ({"placements": []}, "CHECK_FAILED")])
def test_교정이_실패하면_재료를_그대로_돌려준다(materials, body, reply, code):
    llm = ScriptLLM(corrections=[reply, reply])
    c = correct(materials, body["agendas"], body["summary"], llm, 60.0)
    assert c.materials == materials and c.error_code == code and c.applied == 0
    assert len(llm.correction_calls) == (2 if code != "CHECK_FAILED" else 1)    # 호출 실패만 한 번 더, 답 검사는 다시 시키지 않음


def test_고칠_재료가_없으면_AI를_부르지_않는다(materials, body):
    llm = ScriptLLM()
    edited = [m for m in materials if m.edited]
    assert correct(edited, body["agendas"], body["summary"], llm, 60.0).materials == edited and llm.correction_calls == []


# ---- 초안 전체 흐름

def test_초안의_줄_글에_교정이_들어간다(body, materials):
    result = make_draft(body, ScriptLLM(answer(materials), corrections=[RIGHT]))
    assert result["status"] == SUCCEEDED
    content = {s["title"]: s["content"] for s in result["sections"]}
    assert "- 배포 환경: 쿠버네티스(STT: 쿠버내티스) 설정 파일 정리 (박민수, 금요일까지)" in content["할 일"]
    assert "프로메테우스(STT: 프로메테오스) 보관 기간을" in content["안건별 논의"]
    assert "쿠버내티스 버전을 먼저 올려야 한다는 우려" in content["안건별 논의"]          # 사람이 고친 의견은 그대로


def test_칸_고르기에도_교정된_글을_보낸다(body, materials):
    llm = ScriptLLM(answer(materials), corrections=[RIGHT])
    make_draft(body, llm)
    assert "그라파나(STT: 그라파냐) 대시보드 정리" in llm.calls[0][1]
    assert llm.correction_calls[0][0] == load_prompt(kind=CORRECTION_KIND).text


def test_교정이_실패해도_초안은_교정_없이_만든다(body, materials):
    result = make_draft(body, ScriptLLM(answer(materials), corrections=[LLMError("e"), LLMError("e")]))
    assert result["status"] == SUCCEEDED
    assert all("STT:" not in s["content"] for s in result["sections"])


def test_다시_배치에서는_교정하지_않는다(body, materials):
    first = make_draft(body, ScriptLLM(answer(materials), corrections=[RIGHT]))
    again = dict(body, template={"items": TITLES}, previousLines=previous_lines_of(first))
    llm = ScriptLLM({"placements": [{"m": f"m{i}", "section": 1} for i in range(1, 16)], "unplaced": []})
    assert make_draft(again, llm)["status"] == SUCCEEDED and llm.correction_calls == []


def test_로그에는_교정_개수만_남는다(body, materials, caplog):
    caplog.set_level(logging.INFO, logger="minutes.draft")
    make_draft(body, ScriptLLM(answer(materials), corrections=[RIGHT]))
    assert "fixes=3 applied=3 corrected=4" in caplog.text and "correction_prompt=correction-v1" in caplog.text
    assert "쿠버" not in caplog.text and "STT" not in caplog.text


def test_교정이_실패하면_로그에_오류_코드를_남긴다(body, materials, caplog):
    caplog.set_level(logging.INFO, logger="minutes.draft")
    make_draft(body, ScriptLLM(answer(materials), corrections=[LLMTimeout("t"), LLMTimeout("t")]))
    assert "correction_error=LLM_TIMEOUT" in caplog.text and "초안 완료" in caplog.text


# ---- 끄는 설정 (MINUTES_CORRECTION)

@pytest.mark.parametrize("env, on", [({}, True), ({"MINUTES_CORRECTION": "1"}, True), ({"MINUTES_CORRECTION": " 0 "}, False),
                                     ({"MINUTES_CORRECTION": ""}, True)])
def test_교정은_기본으로_켜고_0이면_끈다(env, on):
    from minutes.correction import enabled_from_env
    assert enabled_from_env(env) is on


@pytest.mark.parametrize("raw", ["off", "false", "2", "yes"])
def test_교정_설정이_0이나_1이_아니면_설정_오류(raw):
    from minutes.correction import enabled_from_env
    with pytest.raises(ValueError, match="MINUTES_CORRECTION 는 0 이나 1"):
        enabled_from_env({"MINUTES_CORRECTION": raw})


def test_교정을_끄면_AI를_부르지_않고_글은_그대로다(body, materials, caplog):
    caplog.set_level(logging.INFO, logger="minutes.draft")
    llm = ScriptLLM(answer(materials), corrections=[RIGHT])
    result = make_draft(body, llm, correction=False)
    assert result["status"] == SUCCEEDED and llm.correction_calls == []
    assert all("STT:" not in s["content"] for s in result["sections"])
    assert "correction=off" in caplog.text and "correction_prompt" not in caplog.text
