"""초안 문장 다듬기 (명세 7.8, SCRUM-218). 처음 초안에서 칸 배치·표 채우기 뒤, 줄을 만들기 전에 한 번 한다.

요약 글은 회의 중 화면에서 보도록 쓴 글이라 회의록 문서로는 말끝이 섞이고("~라는 말이 나옴", "~했어요"),
숫자가 한글로 적히고 읽은 값이 따로 붙고(「십이월 사일」(12월 4일로 읽힘)), STT 불확실 표시가 제각각이다.
회의록 문체(개조식 "~함/~음", 아라비아 숫자, 대괄호 표시)로 맞춘다.

  ① 정리 (normalize, 코드)   「X」(Y로 읽힘) → Y. X 의 글이 Y 와 수 낱말로 모두 설명될 때만 바꾼다
                               (「아침 아홉시 이 호선 지옥 같고」(아침 9시, 2호선으로 읽힘) 은 "지옥 같고" 가 Y 에 없어 두고 AI 에 맡긴다).
                               「[테라폼]」처럼 교정 표시만 든 인용은 [테라폼]. ② 가 실패한 줄은 이 글을 쓴다
  ② 다듬기 (AI)               ① 의 글을 줄마다 다시 쓴다. 할 일은 할 일 글만, 결정 확인 필요는 결정 후보 글만 보내고
                               나머지(담당자·기한, 사항 문구)는 코드가 다시 붙인다
  ③ 검사 (check_line, 코드)   걸리면 그 줄만 ① 의 글
                               숫자   다듬은 글의 숫자는 ① 글의 숫자이거나 ① 글의 한글 수 낱말로 읽을 수 있는 값이다.
                                      ① 글의 아라비아 숫자는 모두 남는다 (만·억 단위로 바꿔 쓴 것도 같은 값으로 본다).
                                      다듬은 글의 불확실 표시 [X?] 안에 한글 수로 남은 값은 남은 것으로 본다
                                      (「팔 한 여섯 개」(6개로 읽힘) → [팔 한 여섯 개?]: 읽은 값은 지웠지만 "여섯" 이 남음)
                               표시   ① 글의 [맞는 표기] 는 모두 글자 그대로 남는다. 새 대괄호는 [X?] 이고 X 는 원문 「」 안 글이다
                                      (수 낱말을 숫자로 바꾼 것은 같은 글로 본다: 「팔 한 여섯 개」(6개로 읽힘) → [팔 6개?]).
                                      실제 회의에서 읽은 값이 있는 수를 표시 안에서도 숫자로 바꿨고, 읽기에는 그쪽이 낫다
                                      「」, "(…로 읽힘)", "(STT" 는 남지 않는다 (표시를 대괄호 하나로 맞춘다).
                                      "불확실" 도 원문의 "STT 불확실" 설명 수만큼 줄어야 한다 (설명 글을 남기지 않는다)
                               영어   원문에 없던 영어 낱말이 생기면 걸린다. 실제 회의에서 AI 가 「에이더블유에스」 → AWS,
                                      아이에이 → AI(IA 였음) 처럼 표시 없이 낱말을 바꿨다 (바로잡기는 교정 단계의 일이고 표시가 붙어야 한다)
                               인용   「」 를 지우고 [X?] 로 묶지 않은 채 글을 그대로 두면 걸린다 (수·날짜 인용은 숫자로 바꿔 쓰므로 뺀다).
                                      실제 회의에서 「태로폰」 → 태로폰 처럼 불확실한 말이 확실한 말처럼 남았다
                               낱말   원문 낱말 뒤가 말끝·조사가 아닌 글자로 바뀌면 걸린다 (일 더블유에스 → 일 더블유스).
                                      원문의 "A B" 가 "A 의 앞부분 B" 가 되어도 걸린다 (맡기기 어렵고 → 맡기 어렵고: 뜻이 바뀜)
                               길이   공백을 뺀 글자 수가 ① 글의 MIN_RATIO 미만이면 걸린다 (내용이 빠졌을 가능성)
                               모양   빈 글, 줄바꿈
  실패              호출 실패, 시간 초과, 읽을 수 없는 답, 예상하지 못한 예외는 모든 줄을 ① 의 글로 간다.
                    다듬기 때문에 초안이 실패하지 않게 한다 (오인식 교정과 같음). 답에 없는 줄도 ① 의 글

다듬지 않는 것: 사람이 고친 항목(edited), 다시 배치의 줄(이전 초안 글 그대로, 명세 8장), 표 필드의 재료, 넣지 못한 재료.
끄기: MINUTES_POLISH=0 이면 ② 를 하지 않는다 (① 은 코드라 늘 한다).
AI 에 보내는 글에는 회의 내용이 들어가므로 로그에는 개수만 남긴다 (draft.py).
"""
import logging
import re
from dataclasses import dataclass, field, replace

from .models import Material
from .placement import Prompt, _one_line, _quote, ask, load_prompt

POLISH_KIND = "polish"
POLISH_ERROR = "POLISH_ERROR"
SWITCH_KEY = "MINUTES_POLISH"
MIN_RATIO = 0.5
log = logging.getLogger("minutes.polish")

_READ = re.compile(r"「([^「」]*)」\s*\(([^()]*?)\s*(?:으)?로 읽힘\)")
_QUOTE = re.compile(r"「([^「」]*)」")
_MARK_ONLY = re.compile(r"「\s*(\[[^\[\]]+\])\s*」")
_BRACKET = re.compile(r"\[([^\[\]]+)\]")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)?")
_LEFTOVER = ("「", "」", "읽힘)", "(STT")
_LATIN = re.compile(r"[A-Za-z]+")
_HANGUL = re.compile(r"[가-힣]+")
_STT_NOTE = re.compile(r"STT\s*불확실")
_DATE_WORDS = re.compile(r"[월화수목금토일]요일|오늘|내일|어제|모레|이번주|다음주|지난주|주말|오전|오후|아침|저녁|점심|밤")
# 말끝·조사에 오는 글자. 원문 낱말 뒤에 이 글자만 붙거나 바뀌면 말끝을 바꾼 것으로 본다 (실제 회의 답 165줄로 맞춤)
_ENDING_CHARS = set("다고는은이가를을에의로와과도만기하함음됨임있었겠지며면서아어요게냐니까데해했한할된될인일던든랑나야여것줄수께부터처럼보씩"
                    "라되됐짐져려봄봤봐줌준둠둔씀쓴냄낸으워줬자")

# 한글 수 낱말
_SINO_DIGIT = dict(zip("일이삼사오육칠팔구", range(1, 10)))
_SINO_UNIT = {"십": 10, "백": 100, "천": 1000}
_SINO_BIG = {"만": 10_000, "억": 100_000_000}
_SINO_RUN = re.compile(r"[일이삼사오육칠팔구십백천만억]+")
_NATIVE_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "석": 3, "넷": 4, "네": 4, "넉": 4,
                "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
_NATIVE_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50}
_NATIVE = re.compile("(" + "|".join(sorted(_NATIVE_TENS, key=len, reverse=True)) + ")?("
                     + "|".join(sorted(_NATIVE_ONES, key=len, reverse=True)) + ")?")
_COUNTERS = "개주일월년시분초번명원장건회차살배층칸줄쪽권대곳달"
_SPECIAL = {"일주일": (1, 7), "보름": (15,), "하루": (1,), "이틀": (2,), "사흘": (3,), "나흘": (4,), "열흘": (10,)}


@dataclass(frozen=True)
class Polish:
    materials: list[Material]                   # 다듬은 글이 반영된 재료 (다듬지 않은 재료는 같은 객체)
    sent: int = 0                               # AI 에 보낸 줄 수
    polished: int = 0                           # 검사를 통과해 다듬은 글을 쓴 줄 수
    failed: dict[str, int] = field(default_factory=dict)   # 검사별 걸린 줄 수 (missing = 답에 없음)
    error_code: str | None = None               # ② 없이 간 이유 (LLM_TIMEOUT · LLM_FAILED · CHECK_FAILED · POLISH_ERROR)
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    fallback_calls: int = 0


def enabled_from_env(env) -> bool:
    """MINUTES_POLISH: 없거나 "1" 이면 켬, "0" 이면 끔. 그 밖은 ValueError (교정 설정과 같은 규칙)."""
    raw = (env.get(SWITCH_KEY) or "1").strip()
    if raw not in ("0", "1"):
        raise ValueError(f"{SWITCH_KEY} 는 0 이나 1 이어야 함")
    return raw == "1"


# ---------- ① 정리 ----------

def normalize(text: str) -> str:
    """읽은 값이 붙은 수 인용을 읽은 값으로, 교정 표시만 든 인용을 표시로 바꾼다. 그 밖의 「」 는 그대로 둔다."""
    def read(m):
        inner, reading = m.group(1), m.group(2).strip()
        return reading if _explained(inner, reading) else m.group(0)
    return _MARK_ONLY.sub(r"\1", _READ.sub(read, text))


def _explained(inner: str, reading: str) -> bool:
    """인용 안 글이 읽은 값과 수 낱말·단위로 모두 설명되는가. 남는 한글 글자가 모두 읽은 값에 있어야 한다."""
    rest = _strip_numbers(inner)
    return all(ch in reading for ch in rest if "가" <= ch <= "힣")


def _strip_numbers(text: str) -> str:
    for word in sorted(_SPECIAL, key=len, reverse=True):
        text = text.replace(word, " ")
    text = _SINO_RUN.sub(" ", text)
    text = re.sub("|".join(sorted({**_NATIVE_ONES, **_NATIVE_TENS}, key=len, reverse=True)), " ", text)
    text = re.sub(f"[{_COUNTERS}]|정도|쯤|약|한|여", " ", text)
    return re.sub(r"\s+", "", text)


# ---------- 숫자 ----------

def digit_values(text: str) -> set[float]:
    """아라비아 숫자의 값. 쉼표는 떼고, 바로 뒤의 만·억·천은 곱한다 (300만 = 3000000). 대괄호 안도 센다
    (실제 회의: 「삼십 초코컷」(30으로 읽힘) → [30 초코컷?] 처럼 숫자가 불확실 표시 안으로 들어간다)."""
    out = set()
    for m in _NUMBER.finditer(text):
        raw = m.group(0).replace(",", "").replace(" ", "")
        unit = {"억": 100_000_000, "만": 10_000, "천": 1000}.get(raw[-1], 1)
        num = raw.rstrip("억만천")
        try:
            out.add(float(num) * unit)
        except ValueError:
            continue
    return out


def korean_values(text: str) -> set[float]:
    """글 안 한글 수 낱말로 읽을 수 있는 값 모두. 넓게 잡는다 (다듬은 글에 새로 생긴 숫자를 허락하는 데만 쓴다)."""
    out = set()
    for word, values in _SPECIAL.items():
        if word in text:
            out.update(values)
    # 띄어 쓴 한자 수도 읽는다 (실제 회의: 「십 일월 중순」 → 11월)
    for run in _SINO_RUN.findall(text) + _SINO_RUN.findall(re.sub(r"(?<=[일이삼사오육칠팔구십백천만억]) (?=[일이삼사오육칠팔구십백천만억])", "", text)):
        for i in range(len(run)):
            for j in range(i + 1, min(len(run), i + 8) + 1):
                v = _sino(run[i:j])
                if v is not None:
                    out.add(float(v))
    for m in _NATIVE.finditer(text):
        tens, ones = m.group(1), m.group(2)
        if tens or ones:
            out.add(float(_NATIVE_TENS.get(tens, 0) + _NATIVE_ONES.get(ones, 0)))
    return out


def _sino(word: str) -> int | None:
    """한자어 수 (십이 = 12, 삼백만 = 3000000). 숫자가 연달아 오는 등 수가 아니면 None."""
    total, section, digit = 0, 0, None
    for ch in word:
        if ch in _SINO_DIGIT:
            if digit is not None:
                return None
            digit = _SINO_DIGIT[ch]
        elif ch in _SINO_UNIT:
            section += (digit or 1) * _SINO_UNIT[ch]
            digit = None
        else:                                           # 만·억
            if section == 0 and digit is None and total == 0:
                return None
            total += (section + (digit or 0)) * _SINO_BIG[ch]
            section, digit = 0, None
    return total + section + (digit or 0)


# ---------- ③ 검사 ----------

def check_line(before: str, after: str, original: str) -> str | None:
    """걸린 검사 이름 (shape, leftover, marks, latin, quote, words, numbers, length). 통과하면 None.
    before 는 ① 의 글, after 는 다듬은 글, original 은 ① 전 글 (「」 안 글을 찾는 데 쓴다)."""
    if not after.strip() or "\n" in after.strip():
        return "shape"
    if any(t in after for t in _LEFTOVER):
        return "leftover"
    fixed = [b for b in _BRACKET.findall(before)]
    if any(after.count(f"[{b}]") < before.count(f"[{b}]") for b in set(fixed)):
        return "marks"
    quoted = [_letters(q) for q in _QUOTE.findall(original)]
    for b in _BRACKET.findall(after):
        if b in fixed:
            continue
        if not b.endswith("?") or not b[:-1].strip():
            return "marks"
        inner = _letters(b[:-1])                        # 수만 든 표시([세 개?])는 빈 글: 숫자는 숫자 검사가 본다
        if not any(inner in q for q in quoted):
            return "marks"
    if after.count("불확실") > before.count("불확실") - len(_STT_NOTE.findall(before)):
        return "leftover"
    if not set(_LATIN.findall(after)) <= set(_LATIN.findall(before)):
        return "latin"
    if _unmarked_quote(before, after):
        return "quote"
    if _changed_word(before, after):
        return "words"
    have, allowed = digit_values(before), digit_values(before) | korean_values(before)
    got = digit_values(after)
    kept = {v for b in _BRACKET.findall(after) if b.endswith("?") for v in korean_values(b)}
    if not have <= got | kept or not got <= allowed:
        return "numbers"
    if _length(after) < MIN_RATIO * _length(before):
        return "length"
    return None


def _unmarked_quote(before: str, after: str) -> str | None:
    """「」 를 지웠는데 [X?] 로 묶지 않고 글을 그대로 둔 인용. 실제 회의에서 「태로폰」·「헬름 상태도」를 그대로 써서
    읽는 사람이 확실한 말로 받아들였다. 수·날짜 인용은 숫자로 바꿔 쓰므로 빼고, 사람 말은 풀어 쓰면 글이 달라지므로 걸리지 않는다."""
    outside = _flat(_BRACKET.sub(" ", after))
    rest = _flat(_QUOTE.sub(" ", before))                # 인용 밖에도 있는 말은 원래 글에 있던 것
    for q in _QUOTE.findall(before):
        letters = _letters(q)
        values = korean_values(q)
        if values and values & digit_values(after):     # 「십 일월 중순에서 말 사이」 → 11월 중순에서 말 사이
            continue
        if not _DATE_WORDS.sub("", letters):            # 「금요일」
            continue
        if len(letters) >= 2 and letters not in rest and letters in outside:
            return q
    return None


def _changed_word(before: str, after: str) -> str | None:
    """원문 낱말 뒤가 말끝이 아닌 글자로 바뀐 낱말. 실제 회의: 일 더블유에스 → 일 더블유스.
    앞 두 글자 이상이 원문에 있고, 원문에 없는 뒤 글자가 말끝·조사 글자가 아니면 표기를 바꾼 것으로 본다.
    뒤 글자를 지운 것은 말끝을 줄인 것과 구별하기 어려워, 바로 뒤 낱말이 그대로일 때만 본다
    (맡기기 어렵고 → 맡기 어렵고: 남에게 맡기는 것이 내가 맡는 것으로 바뀜. 실제 회의 10판 중 7판)."""
    source = _flat(before.replace("「", "").replace("」", ""))
    after_words = _words(after)
    for word in after_words:
        stem = next((n for n in range(len(word), 0, -1) if word[:n] in source), 0)
        if 2 <= stem < len(word) and not set(word[stem:]) <= _ENDING_CHARS:
            return word
    before_words = _words(before.replace("「", " ").replace("」", " "))
    pairs = set(zip(after_words, after_words[1:]))
    for word, nxt in zip(before_words, before_words[1:]):
        for short, same in pairs:
            if same == nxt and short != word and len(short) >= 2 and word.startswith(short):
                return short
    return None


def _words(text: str) -> list[str]:
    """대괄호 밖 한글 낱말."""
    return _HANGUL.findall(_BRACKET.sub(" ", text))


def _flat(text: str) -> str:
    return re.sub(r"[\s\d,.~]", "", text)


def _letters(text: str) -> str:
    """수·단위로만 된 낱말과 숫자, 띄어쓰기를 뺀 글. 불확실 표시 안 글이 원문 「」 안 글과 같은지 볼 때 쓴다.
    낱말 안의 글자는 지우지 않는다 ("약을 사" 의 "사" 는 남고, 따로 선 "여섯"·"6개" 는 빠진다)."""
    kept = [w for w in text.split() if _strip_numbers(re.sub(r"[\d,.]", "", w))]
    return "".join(re.sub(r"[\d,.]", "", w) for w in kept)


def _length(text: str) -> int:
    return len(re.sub(r"\s", "", text))


# ---------- ② 다듬기 ----------

_TODO = re.compile(r"^(.*\S) (\([^()]*\))$")                       # 할 일 (담당자, 기한[, 할 일 확인 필요])
_CHECK = re.compile(r"^(.*?)(\s*\(결정 확인 필요: )(.*)\)$")         # 사항 문구 (결정 확인 필요: 결정 후보)
KIND_LABELS = {"decision": "결정", "undecided": "미결", "decisionCheck": "결정 후보", "summary": "요약",
               "opinion": "의견", "todo": "할 일"}


def split(m: Material) -> tuple[str, str, str]:
    """(앞, 다듬을 글, 뒤). 할 일은 담당자·기한을, 결정 확인 필요는 사항 문구와 틀을 코드가 붙이므로 떼어 낸다."""
    if m.kind == "todo":
        hit = _TODO.match(m.text)
        if hit:
            return "", hit.group(1), " " + hit.group(2)
    if m.kind == "decisionCheck":
        hit = _CHECK.match(m.text)
        if hit:
            return hit.group(1) + hit.group(2), hit.group(3), ")"
    return "", m.text, ""


def targets(materials: list[Material], refs: set[str]) -> list[Material]:
    """다듬을 재료: refs(글 필드에 놓인 재료) 가운데 사람이 고치지 않았고 요약 항목에서 온 처음 초안의 재료."""
    return [m for m in materials if m.ref in refs and not m.edited and m.sourced and m.previous is None
            and m.kind in KIND_LABELS]


def polish(materials: list[Material], refs: set[str], llm, timeout_s: float, prompt: Prompt | None = None,
           call_retries: int = 1, enabled: bool = True) -> Polish:
    """refs 는 글 필드에 놓인 재료 번호. ① 은 늘 하고, enabled 면 ② ③ 을 한다. 실패해도 예외 없이 ① 의 글로 돌려준다."""
    chosen = targets(materials, refs)
    if not chosen:
        return Polish(list(materials))
    parts = {m.ref: split(m) for m in chosen}
    before = {ref: normalize(body) for ref, (_, body, _) in parts.items()}
    if not enabled:
        return Polish(_apply(materials, parts, before))
    try:
        return _polish(materials, chosen, parts, before, llm, timeout_s, prompt, call_retries)
    except Exception as e:                               # 다듬기 때문에 초안이 실패하지 않게 한다
        log.warning("다듬기 건너뜀 error=%s", type(e).__name__)
        return Polish(_apply(materials, parts, before), sent=len(chosen), error_code=POLISH_ERROR)


def _polish(materials, chosen, parts, before, llm, timeout_s, prompt, call_retries) -> Polish:
    outcome = ask(prompt or load_prompt(kind=POLISH_KIND), lambda _: build_user(chosen, before), read_lines,
                  llm, timeout_s, max_retries=0, call_retries=call_retries)
    usage = dict(attempts=outcome.attempts, input_tokens=outcome.input_tokens, output_tokens=outcome.output_tokens,
                 fallback_calls=outcome.fallback_calls)
    if outcome.placement is None:
        return Polish(_apply(materials, parts, before), sent=len(chosen), error_code=outcome.error_code, **usage)
    answers, texts, failed = outcome.placement, dict(before), {}
    for m in chosen:
        after = _unlabel(answers.get(m.ref), m)
        problem = "missing" if after is None else check_line(before[m.ref], after, parts[m.ref][1])
        if problem:
            failed[problem] = failed.get(problem, 0) + 1
        else:
            texts[m.ref] = after.strip()
    return Polish(_apply(materials, parts, texts), sent=len(chosen), polished=len(chosen) - sum(failed.values()),
                  failed=failed, **usage)


def _unlabel(text: str | None, m: Material) -> str | None:
    """답 앞에 따라 쓴 번호·종류("m3 | 요약 | ", "[요약] ", "요약 | ")를 뗀다."""
    if text is None:
        return None
    label = re.escape(KIND_LABELS[m.kind])
    return re.sub(rf"^\s*(?:{re.escape(m.ref)}\s*\|\s*)?(?:\[{label}\]\s*|{label}\s*\|\s*)", "", text)


def build_user(chosen: list[Material], before: dict[str, str]) -> str:
    """AI 에게 줄 글. 꺾쇠는 글 안에서 바꿔 칸 밖으로 새지 않게 한다 (배치와 같음)."""
    # 종류를 대괄호로 쓰지 않는다: 답에 "[요약] …" 처럼 따라 써서 교정 표시와 헷갈렸다 (실제 회의 33줄 모두)
    lines = [f"{m.ref} | {KIND_LABELS[m.kind]} | {_quote(_one_line(before[m.ref]))}" for m in chosen]
    return "\n".join(["<줄>", *lines, "</줄>"])


def read_lines(data) -> tuple[dict[str, str] | None, list[str]]:
    """답 → 줄 번호 → 다듬은 글. 모양이 틀린 항목은 뺀다(그 줄은 ① 의 글). 답 전체를 읽을 수 없을 때만 None."""
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None, ['답이 {"lines": [...]} 모양의 JSON이 아님']
    out = {}
    for item in data["lines"]:
        if isinstance(item, dict) and isinstance(item.get("m"), str) and isinstance(item.get("text"), str):
            out.setdefault(item["m"], item["text"])
    return out, []


def _apply(materials, parts, texts) -> list[Material]:
    out = []
    for m in materials:
        if m.ref in parts:
            head, _, tail = parts[m.ref]
            text = head + texts[m.ref] + tail
            out.append(m if text == m.text else replace(m, text=text))
        else:
            out.append(m)
    return out
