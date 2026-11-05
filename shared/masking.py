"""외부 AI 로 보내는 글의 개인정보 가리기. 보내기 전에 가리고(mask), 답을 받으면 되돌린다(unmask).

공통 AI 연결(shared/llm.py 의 MaskingLLM)과 요약의 호출 함수(masked_call)가 같이 쓴다.
요약 폴더 안에서 직접 돌리는 도구도 이 파일을 불러오도록 표준 라이브러리만 쓴다.

형식이 뚜렷한 값이 나가는 것을 줄이는 기능이고, 개인정보가 나가지 않는다는 보장은 아니다.
가리는 값: 주민등록번호, 카드번호, 이메일, IPv4 주소, 전화번호, 계좌번호, 번지까지 있는 주소, 차량번호,
8자리 이상 숫자와 1234-5678 모양, 한글로 7자 이상 읽은 번호, 비밀번호·인증번호·생년월일이라는 말 바로 뒤의 숫자.
찾지 못하는 것: 사람 이름, 단서 없는 짧은 숫자(뒷 4자리, 우편번호, 동·호수), "천이백삼십사"처럼 자릿수로 읽은 번호,
두 줄로 나뉜 번호, 번지 없이 지역만 말한 주소, IPv6.

대체 표시는 <PHONE_1> 모양이다. "[n]"은 RAG 근거 번호, "⟨ ⟩"는 STT 불확실 표시라 쓰지 않는다.
대응표는 호출마다 새로 만들고 메모리에만 둔다. 로그에는 종류별 개수만 남긴다.
"""
import functools
import logging
import re
from collections import Counter

log = logging.getLogger("shared.masking")

_ACCOUNT_MIN_DIGITS = 10        # 날짜(2026-11-05)는 8자리라 계좌번호로 잡지 않는다
_SPOKEN_MIN_DIGITS = 7          # STT 가 한글로 적은 번호. 앞자리를 빼고 뒷번호만 읽은 경우(7~8자)까지 잡는 길이
_PHONE_DIGITS = 11
_SPOKEN_MIN_HANGUL = 4          # 한글 숫자가 이만큼은 있어야 한다. 그냥 긴 숫자(ID 등)는 말로 읽은 번호가 아니다
# 번호를 부를 때 쓰는 말. 일·이는 소리가 비슷해 하나·둘로 바꿔 말하는 경우가 있다
_SPOKEN_DIGITS = {"공": "0", "영": "0", "빵": "0", "일": "1", "하나": "1", "이": "2", "둘": "2", "삼": "3", "셋": "3",
                  "사": "4", "넷": "4", "오": "5", "다섯": "5", "육": "6", "륙": "6", "여섯": "6", "칠": "7", "일곱": "7",
                  "팔": "8", "여덟": "8", "구": "9", "아홉": "9"}
_SPOKEN_HANGUL = "|".join(sorted(_SPOKEN_DIGITS, key=len, reverse=True))     # "일곱"을 "일"보다 먼저 본다
_SPOKEN_ONE = _SPOKEN_HANGUL + "|[0-9]"
_SPOKEN_WORD = re.compile(_SPOKEN_ONE)
_SPOKEN_GAP = r"(?:[ \t\-⟨⟩에의]|다시){0,3}"      # 하이픈을 소리 내어 읽은 말도 넘어간다. 줄바꿈은 넘지 않는다

_SIDO = "(?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|충청북|충청남|전북|전남|전라북|전라남|경북|경남|경상북|경상남|제주)[가-힣]{0,6}"
_DISTRICT = "[가-힣]{1,6}(?:시|군|구)"
_ROAD = r"[가-힣0-9]{1,12}(?<!으)(?:대로|로|길)(?:\s*\d+번?길)?"        # "앞으로 3개" 같은 말은 도로명이 아니다
_LOT = "[가-힣0-9]{1,8}(?:동|읍|면|리)"
_NOT_JOSA = "(?!(?<=이)[가-힣])"     # 번지 끝의 "이" 뒤에 한글이 이어지면 조사로 본다 ("백이십삼이다")
_HOUSE = rf"(?:\d+|[공영일이삼사오육륙칠팔구십백천]+{_NOT_JOSA})(?:-\d+)?(?:번지)?"
_NOT_COUNT = r"(?!\d|\s*(?:개|명|원|분|시|일|월|년|번|회|건|퍼센트|%))"
# 날짜(20261105)와 연도 범위(2020-2025)는 길이가 맞아도 번호가 아니다
_DATE_LIKE = re.compile(r"(?:19|20)\d\d(?:-(?:19|20)\d\d|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))")
# 단서 말 바로 뒤의 짧은 숫자. 길이로는 금액·수량과 구분되지 않아 앞의 말로 판단한다
_CLUE_JOSA = r"\s*(?:은|는|이|가|을|를|도|:|=)?\s*"
_CLUE_NUMBER = rf"\d{{4,}}(?:-\d+)*{_NOT_COUNT}|(?:{_SPOKEN_HANGUL})(?:{_SPOKEN_GAP}(?:{_SPOKEN_HANGUL})){{3,}}"
_BIRTH_DATE = r"\d{2,4}[-./]\d{1,2}[-./]\d{1,2}|\d{2,4}년\s*\d{1,2}월\s*\d{1,2}일"
_PLATE_LETTER = "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주아바사자배하허호"


def _account(value):
    return ("ACCOUNT", value) if sum(ch.isdigit() for ch in value) >= _ACCOUNT_MIN_DIGITS else None


def _long_number(value):
    return None if _DATE_LIKE.fullmatch(value) else ("NUMBER", value)


def _spoken(value):
    words = _SPOKEN_WORD.findall(value)
    digits = "".join(_SPOKEN_DIGITS.get(w, w) for w in words)
    if len(digits) < _SPOKEN_MIN_DIGITS or sum(w in _SPOKEN_DIGITS for w in words) < _SPOKEN_MIN_HANGUL:
        return None
    if not digits.startswith("01"):
        return "NUMBER", value
    # 끝의 "이"는 숫자 2일 수도 조사일 수도 있어 길이로 정한다. 휴대폰 번호는 11자리라 12자리면 조사다 ("오육칠팔이에요").
    # 그 밖에는 가리는 쪽을 고른다. 문장이 조금 어색해져도 번호가 새는 것보다 낫다
    if len(digits) == _PHONE_DIGITS + 1 and words[-1] == "이":
        return "PHONE", value[:-1]
    return "PHONE", value


def _always(kind):
    return lambda value: (kind, value)


# 위에서부터 차례로 적용한다. 앞 패턴이 가린 값은 뒤 패턴에 걸리지 않는다.
# 판별 함수는 (종류, 가릴 부분)을 주고, None 을 주면 그대로 둔다. 가릴 부분 뒤의 글자는 남는다.
# head 묶음은 단서 말이라 가리지 않고 남긴다. AI 가 무슨 값인지는 알아야 글을 쓸 수 있다
_PATTERNS = (
    (_always("RRN"), re.compile(r"(?<![\d-])\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[- ]?[1-8]\d{6}(?![\d-])")),
    (_always("CARD"), re.compile(r"(?<![\d-])(?:\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}|\d{4}[- ]?\d{6}[- ]?\d{5})(?![\d-])")),
    (_always("EMAIL"), re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    (_always("IP"), re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d|\.\d)")),
    (_always("PHONE"), re.compile(r"(?<![\d-])(?:01[016789][-. ]?\d{3,4}[-. ]?\d{4}|0\d{1,3}-\d{3,4}-\d{4})(?![\d-])")),
    (_account, re.compile(r"(?<![\d-])\d{2,6}(?:-\d{2,8}){1,3}(?![\d-])")),
    # 단서 말 패턴보다 먼저 둔다. "비밀번호는 1234-5678" 에서 앞 네 자리만 가려지지 않게 한다
    (_long_number, re.compile(rf"(?<![\d:.,-])(?:\d{{4}}-\d{{4}}(?![\d-])|\d{{8,}}{_NOT_COUNT})")),
    (_always("SECRET"), re.compile(
        rf"(?P<head>(?:비밀\s?번호|비번|인증\s?번호|핀\s?번호|(?<![A-Za-z])(?i:PIN)){_CLUE_JOSA})(?:{_CLUE_NUMBER})")),
    (_always("BIRTH"), re.compile(
        rf"(?P<head>(?:생년월일|(?<![가-힣])생일){_CLUE_JOSA})(?:{_BIRTH_DATE}|{_CLUE_NUMBER})")),      # "발생일"은 생일이 아니다
    (_always("CAR"), re.compile(rf"(?<!\d)\d{{2,3}}[{_PLATE_LETTER}]\s?\d{{4}}{_NOT_COUNT}")),
    # 줄바꿈은 구분 글자가 아니다 (전사 줄을 넘어서 잇지 않는다). 앞의 조건은 줄 머리의 시간 "03:21" 을 시작으로 삼지 않게 한다
    (_spoken, re.compile(rf"(?<![\d:.,])(?:{_SPOKEN_ONE})(?:{_SPOKEN_GAP}(?:{_SPOKEN_ONE}))+")),
    (_always("ADDRESS"), re.compile(
        rf"(?<![가-힣])(?:{_SIDO}\s+(?:{_DISTRICT}\s+){{0,2}}|(?:{_DISTRICT}\s+){{1,2}})"
        rf"(?:{_ROAD}|{_LOT})\s*{_HOUSE}{_NOT_COUNT}(?:[, ]*\d+동\s*\d+호)?")),
)
_KINDS = "RRN|CARD|EMAIL|IP|PHONE|ACCOUNT|NUMBER|SECRET|BIRTH|CAR|ADDRESS"
_TOKEN = re.compile(rf"<(?:{_KINDS})_\d+>")
_LEFTOVER = re.compile(rf"(?<![A-Za-z0-9_])(?:{_KINDS})_\d+")      # AI 가 꺾쇠를 빼고 쓴 대체 표시도 센다


def mask(text: str) -> tuple[str, dict[str, str]]:
    """개인정보를 대체 표시로 바꾼다. 반환: (가린 글, {대체 표시: 원래 값}). 같은 값은 같은 번호를 쓴다."""
    table: dict[str, str] = {}
    tokens: dict[str, str] = {}
    counters: Counter = Counter()
    original = text

    def swap(m, kind_of):
        head = m.group("head") if "head" in m.re.groupindex else ""
        body = m.group(0)[len(head):]
        found = kind_of(body)
        if found is None:
            return m.group(0)
        kind, value = found
        rest = body[len(value):]
        if value not in tokens:
            counters[kind] += 1
            while f"<{kind}_{counters[kind]}>" in original:      # 원문에 같은 글자가 있으면 번호를 건너뛴다
                counters[kind] += 1
            tokens[value] = f"<{kind}_{counters[kind]}>"
            table[tokens[value]] = value
        return head + tokens[value] + rest

    for kind_of, pattern in _PATTERNS:
        text = pattern.sub(lambda m: swap(m, kind_of), text)
    if table:
        counts = Counter(token[1:].rsplit("_", 1)[0] for token in table)
        log.info("개인정보 가림: %s", " ".join(f"{k}={n}" for k, n in sorted(counts.items())))
    return text, table


def unmask(text: str, table: dict[str, str]) -> str:
    """대체 표시를 원래 값으로 되돌린다. 정확히 일치하는 것만 되돌리고, 못 되돌린 개수는 로그에 남긴다."""
    if not table:
        return text
    restored = _TOKEN.sub(lambda m: table.get(m.group(0), m.group(0)), text)
    left = len(_LEFTOVER.findall(restored))
    if left:
        log.warning("대체 표시를 되돌리지 못함: count=%d", left)
    return restored


def masked_call(fn):
    """fn(provider, model, system, user, ...) 모양의 호출 함수를 감싼다. user 만 가리고, 돌아온 글을 되돌린다.
    가리다 오류가 나면 fn 을 부르지 않고 그대로 던진다 (원문을 보내지 않는다)."""
    @functools.wraps(fn)
    def wrapper(provider, model, system, user, *args, **kwargs):
        masked, table = mask(user)
        return unmask(fn(provider, model, system, masked, *args, **kwargs), table)
    return wrapper
