"""STT 오인식 교정 (명세 7.7). 처음 초안에서 재료를 만든 뒤, 칸을 고르기 전에 한 번 한다.

  AI 에게 주는 글   <안건> 안건 제목과 결정할 사항 문구, <참고 글> 재료에서 빠진 의견, <재료> m 번호마다 글
  AI 의 답          {"fixes": [{"wrong": "테로폰", "right": "테라폼"}, {"wrong": "태로폰", "right": "Terraform", "reading": "테라폼"}]}.
                    글을 다시 쓰지 않고 쌍만 낸다. 맞는 표기에 영어가 있으면 한글로 읽는 소리(reading)도 낸다
  쌍 검사           하나라도 걸리면 그 쌍만 버린다 (check_fix)
                    wrong   사람이 고치지 않은 재료 글에 있다
                    right   출처에 글자 그대로 있다. 출처는 요청 요약 안의 모든 글(요약 문장, 의견, 할 일, 결정 내용)의
                            「」 밖, 안건 제목, 결정할 사항 문구. 재료에서 빠진 의견(요약 문장이 담은 것, materials.py)도
                            요청에 있는 글이라 출처다 (실제 회의 2026-11-03: 「」 밖의 "테라폼"이 빠진 의견에만 있었음).
                            「」 안은 요약 쪽이 STT 를 확신하지 못해 원문 그대로 옮긴 글이라 출처로 쓰지 않는다
                    영어    right 에 영어가 있으면 사용자가 쓴 안건 제목·결정할 사항에 글자 그대로 있을 때만 받는다
                            (요약 글의 영어는 요약 단계가 쓴 표기라 출처로 쓰지 않는다). 소리는 reading 으로 비교하고,
                            reading 이 wrong 과 같으면 오인식이 아니라 표기만 바꾸는 것이라 버린다 (에이더블유에스 → AWS).
                            실제 회의 안건이 "AWS·Terraform", "Helm 차트" 처럼 영어로 적혀 있었다 (2026-11-04)
                    글자    wrong·reading 은 한글(띄어쓰기 포함)만, right 는 한글·영어·띄어쓰기만. 두 글자 이상, 서로 다르고
                            한쪽이 다른 쪽을 품지 않는다, 숫자·수 낱말이 없다
                    소리    자모로 풀어 비교한 비슷함이 글자 수가 같으면 MIN_SIMILARITY, 다르면(MAX_LENGTH_GAP 자까지)
                            MIN_SIMILARITY_GAP 이상 (뜻을 바꾸는 교정을 막는다). STT 는 글자 수를 바꿔 적기도 한다
                            (실제 회의: 「기능면 시선」 ← 기능 명세). 글자 수가 다르면 뜻이 다른 말이 끼기 쉬워 기준을 높인다
                            (요고사항 문서 / 요구사항 0.57, 백앤드 서버 / 백엔드 0.58 은 막고 기능면 시선 / 기능 명세 0.62 는 받음)
  반영              사람이 고치지 않은 재료의 글과 표 조각(parts)에서 wrong 을 "right(STT: wrong)" 로 바꾼다.
                    「」 안이든 밖이든 같은 모양이다. 원문이 남으므로 표 검사(「」 표시)와도 어긋나지 않는다
  생각하는 양      EFFORT. None 이면 모델 기본값. 실제 회의 요약에서 기본값 20.5~29.9초, "low" 12.0~14.3초였지만 (각 3번)
                    "low" 는 덜 잡아(합성 사례 교정 맞음 4/10판) 기본값을 쓴다. 초안은 콜백으로 알려 사용자가 바로 기다리지 않는다
  실패              호출 실패, 시간 초과, 읽을 수 없는 답은 다시 시키지 않고 교정 없이 간다. 교정 때문에 초안이 실패하지 않게 한다.
                    예상하지 못한 예외(코드 버그 등)도 교정만 건너뛴다 (CORRECTION_ERROR, 로그에는 예외 이름만)

끄기              MINUTES_CORRECTION=0 (enabled_from_env). 요약 단계가 오인식을 충분히 잡게 되면 배포 설정만으로 끈다.
                    없거나 1 이면 켠다. 그 밖의 값은 설정 오류다 (끄려던 값이 조용히 켜진 채 돌지 않게)

다시 배치(명세 8장)에서는 하지 않는다. 이전 줄은 이미 교정된 글이거나 사람이 고친 글이다.
AI 에 보내는 글과 쌍에는 회의 내용이 들어가므로 로그에는 개수만 남긴다 (draft.py).
"""
import logging
import re
from dataclasses import dataclass, replace

from .materials import agenda_points, opinion_covered, summary_seqs
from .models import Material
from .placement import Prompt, _one_line, _quote, ask, load_prompt

CORRECTION_KIND = "correction"
CORRECTION_ERROR = "CORRECTION_ERROR"
SWITCH_KEY = "MINUTES_CORRECTION"
log = logging.getLogger("minutes.correction")
MIN_SIMILARITY = 0.5
MIN_SIMILARITY_GAP = 0.6                # 글자 수가 다를 때
MAX_LENGTH_GAP = 2                      # 글자 수 차이 상한 (띄어쓰기는 세지 않음)
EFFORT: str | None = None
MAX_FIXES = 20                          # 답에서 읽는 쌍 수 상한. 넘는 것은 버린다
_QUOTED = re.compile(r"「[^」]*」")
_HANGUL = re.compile(r"[가-힣]+(?: [가-힣]+)*")
_WRITTEN = re.compile(r"[가-힣A-Za-z]+(?: [가-힣A-Za-z]+)*")          # 맞는 표기: 한글·영어
_LATIN = re.compile(r"[A-Za-z]")
_NUMBER = re.compile(r"(?:한|두|세|네|하나|둘|셋|넷|다섯|여섯|일곱|여덟|아홉|열|스물|서른|마흔|쉰|"
                     r"일|이|삼|사|오|육|칠|팔|구|십|백|천|만)+"
                     r"(?:개|주|명|시|분|초|번|일|달|년|월|시간|주일|퍼센트|차|살|배|등)?")


@dataclass(frozen=True)
class Correction:
    materials: list[Material]           # 교정이 반영된 재료 (바뀌지 않은 재료는 같은 객체)
    fixes: int = 0                      # 답에서 읽은 쌍 수
    applied: int = 0                    # 검사를 통과한 쌍 수
    changed: int = 0                    # 글이 바뀐 재료 수
    error_code: str | None = None       # 교정 없이 간 이유 (LLM_TIMEOUT · LLM_FAILED · CHECK_FAILED)
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_calls: int = 0


def enabled_from_env(env) -> bool:
    """MINUTES_CORRECTION: 없거나 "1" 이면 켬, "0" 이면 끔. 그 밖은 ValueError (서버는 설정 오류로 초안 주소만 503)."""
    raw = (env.get(SWITCH_KEY) or "1").strip()
    if raw not in ("0", "1"):
        raise ValueError(f"{SWITCH_KEY} 는 0 이나 1 이어야 함")
    return raw == "1"


def correct(materials: list[Material], agendas: list[dict], summary: dict, llm, timeout_s: float,
            prompt: Prompt | None = None, call_retries: int = 1) -> Correction:
    """재료가 1개 이상일 때 부른다. agendas·summary 는 요청의 값. 실패해도 예외 없이 받은 재료를 그대로 돌려준다."""
    try:
        return _correct(materials, agendas, summary, llm, timeout_s, prompt, call_retries)
    except Exception as e:                               # 교정 때문에 초안이 실패하지 않게 한다
        log.warning("교정 건너뜀 error=%s", type(e).__name__)
        return Correction(materials, error_code=CORRECTION_ERROR)


def _correct(materials, agendas, summary, llm, timeout_s, prompt, call_retries) -> Correction:
    targets = [m for m in materials if not m.edited]
    if not targets:                                      # 고칠 수 있는 재료가 없으면 AI 를 부르지 않는다
        return Correction(materials)
    sources, written = source_text(summary, agendas), agenda_text(agendas)
    outcome = ask(prompt or load_prompt(kind=CORRECTION_KIND), lambda _: build_user(materials, agendas, summary),
                  read_fixes, llm, timeout_s, max_retries=0, call_retries=call_retries, effort=EFFORT)
    usage = dict(attempts=outcome.attempts, input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                 fallback_calls=outcome.fallback_calls)
    if outcome.placement is None:
        return Correction(materials, error_code=outcome.error_code, **usage)
    pairs = outcome.placement
    accepted = [p[:2] for p in pairs if check_fix(*p, targets, sources, written)]
    fixed = apply(materials, accepted)
    changed = sum(a is not b for a, b in zip(materials, fixed))
    return Correction(fixed, fixes=len(pairs), applied=len(accepted), changed=changed, **usage)


def build_user(materials: list[Material], agendas: list[dict], summary: dict) -> str:
    """AI 에게 줄 글. 꺾쇠는 글 안에서 바꿔 칸 밖으로 새지 않게 한다 (배치와 같음). 사람이 고친 재료는 보내지 않는다.
    재료에서 빠진 의견은 <참고 글> 로 보여 준다. 바꿀 대상이 아니고 맞는 표기를 찾는 데만 쓴다."""
    parts = ["<안건>"]
    for a in agendas:
        parts.append(f"- {_quote(_one_line(a['title']))}")
        parts += [f"  결정할 사항: {_quote(_one_line(p['text']))}" for p in agenda_points(a) if p["text"].strip()]
    parts.append("</안건>")
    left_out = dropped_opinions(summary)
    if left_out:
        parts += ["<참고 글>", *(f"- {_quote(_one_line(t))}" for t in left_out), "</참고 글>"]
    parts.append("<재료>")
    parts += [f"{m.ref} {_quote(_one_line(m.text))}" for m in materials if not m.edited]
    parts.append("</재료>")
    return "\n".join(parts)


def read_fixes(data) -> tuple[list[tuple[str, str, str | None]] | None, list[str]]:
    """답 → (wrong, right, reading) 목록. reading 은 없으면 None. 모양이 틀린 항목은 뺀다. 답 전체를 읽을 수 없을 때만 None."""
    if not isinstance(data, dict) or not isinstance(data.get("fixes"), list):
        return None, ['답이 {"fixes": [...]} 모양의 JSON이 아님']
    pairs = []
    for f in data["fixes"][:MAX_FIXES]:
        if isinstance(f, dict) and isinstance(f.get("wrong"), str) and isinstance(f.get("right"), str):
            reading = f.get("reading").strip() if isinstance(f.get("reading"), str) and f["reading"].strip() else None
            pair = (f["wrong"].strip(), f["right"].strip(), reading)
            if pair not in pairs:
                pairs.append(pair)
    return pairs, []


def dropped_opinions(summary: dict) -> list[str]:
    """요약 문장이 이미 담아 재료가 되지 않은 의견의 글 (materials.build_materials 와 같은 규칙)."""
    out = []
    for agenda in summary.get("agendas") or []:
        covered = summary_seqs(agenda)
        out += [o["text"] for o in agenda.get("opinions") or [] if opinion_covered(o, covered)]
    return out


def source_text(summary: dict, agendas: list[dict]) -> str:
    """맞는 표기의 출처. 요청 요약의 모든 글(요약 문장, 의견, 할 일, 결정 내용. 사람이 고친 것 포함)에서 「」 안을 지운 것,
    안건 제목, 결정할 사항 문구."""
    texts = []
    for agenda in summary.get("agendas") or []:
        texts += [i["text"] for key in ("summary", "opinions") for i in agenda.get(key) or []]
        texts += [t["task"] for t in agenda.get("todos") or []]
        texts += [p.get("decisionText") or "" for p in agenda.get("decisionPoints") or []]
    texts = [_QUOTED.sub(" ", t) for t in texts]
    return "\n".join(texts + [agenda_text(agendas)])


def agenda_text(agendas: list[dict]) -> str:
    """사용자가 회의 전에 쓴 글: 안건 제목, 결정할 사항 문구. 영어 맞는 표기는 여기서만 찾는다."""
    texts = []
    for a in agendas:
        texts.append(a["title"])
        texts += [p["text"] for p in agenda_points(a)]
    return "\n".join(texts)


def check_fix(wrong: str, right: str, reading: str | None, targets: list[Material], sources: str,
              written: str) -> bool:
    """sources 는 맞는 표기의 출처 전체(source_text), written 은 사용자가 쓴 안건 글(agenda_text)."""
    english = bool(_LATIN.search(right))
    if english:
        if not (reading and _HANGUL.fullmatch(reading) and _WRITTEN.fullmatch(right)) or reading == wrong:
            return False
        if right not in written:
            return False
        heard = reading
    else:
        if not _HANGUL.fullmatch(right) or right not in sources:
            return False
        heard = right
    if not _HANGUL.fullmatch(wrong) or not any(wrong in m.text for m in targets):
        return False
    if wrong in heard or heard in wrong or _syllables(wrong) < 2 or _syllables(heard) < 2:
        return False
    if _has_number(wrong) or _has_number(heard):
        return False
    gap = abs(_syllables(wrong) - _syllables(heard))
    if gap > MAX_LENGTH_GAP:
        return False
    return similarity(wrong, heard) >= (MIN_SIMILARITY if gap == 0 else MIN_SIMILARITY_GAP)


def apply(materials: list[Material], pairs: list[tuple[str, str]]) -> list[Material]:
    """사람이 고치지 않은 재료의 글과 조각에서 wrong → "right(STT: wrong)". 긴 wrong 부터 한 번에 바꿔 겹쳐 바꾸지 않는다."""
    if not pairs:
        return list(materials)
    to = dict(pairs)
    pattern = re.compile("|".join(re.escape(w) for w in sorted(to, key=len, reverse=True)))

    def sub(text: str) -> str:
        return pattern.sub(lambda m: f"{to[m.group()]}(STT: {m.group()})", text)

    out = []
    for m in materials:
        if m.edited or not pattern.search(m.text):
            out.append(m)
            continue
        out.append(replace(m, text=sub(m.text), parts=tuple((name, sub(value)) for name, value in m.parts)))
    return out


def similarity(a: str, b: str) -> float:
    """자모로 풀어 편집 거리로 잰 비슷함 (0~1). 띄어쓰기는 보지 않는다. 예: 테로폰·테라폼 0.71, 태로폰·테라폼 0.57."""
    x, y = _jamo(a), _jamo(b)
    return 1 - _distance(x, y) / max(len(x), len(y), 1)


def _jamo(text: str) -> list[str]:
    out = []
    for ch in text.replace(" ", ""):
        code = ord(ch) - 0xAC00
        cho, jung, jong = code // 588, code % 588 // 28, code % 28
        out += [f"c{cho}", f"v{jung}"] + ([f"j{jong}"] if jong else [])
    return out


def _distance(x: list[str], y: list[str]) -> int:
    row = list(range(len(y) + 1))
    for i, a in enumerate(x, 1):
        prev, row[0] = row[0], i
        for j, b in enumerate(y, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (a != b))
    return row[-1]


def _syllables(text: str) -> int:
    return len(text.replace(" ", ""))


def _has_number(text: str) -> bool:
    return any(ch.isdigit() for ch in text) or any(_NUMBER.fullmatch(t) for t in text.split())
