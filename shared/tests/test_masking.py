"""개인정보 가리기 시험. 값은 모두 합성이고 실제 AI 는 부르지 않는다."""
import logging

import pytest

from shared import masking
from shared.llm import FallbackLLM, LLMError, MaskingLLM, Reply, add_fallback, make_llm
from shared.masking import mask, masked_call, unmask

OPENAI_ENV = {"RAG_LLM_PROVIDER": "openai", "RAG_LLM_MODEL": "a", "OPENAI_API_KEY": "k" * 20}


# ---- 찾기 ----

@pytest.mark.parametrize("value, token", [
    ("900101-1234567", "<RRN_1>"),
    ("9001011234567", "<RRN_1>"),
    ("kim.test+1@example.co.kr", "<EMAIL_1>"),
    ("010-1234-5678", "<PHONE_1>"),
    ("01012345678", "<PHONE_1>"),
    ("010 1234 5678", "<PHONE_1>"),
    ("02-123-4567", "<PHONE_1>"),
    ("110-123-456789", "<ACCOUNT_1>"),
    ("1002-123-456789", "<ACCOUNT_1>"),
    ("1234-5678-9012-3456", "<CARD_1>"),
    ("1234567890123456", "<CARD_1>"),
    ("3782-822463-10005", "<CARD_1>"),
    ("192.168.0.12", "<IP_1>"),
    ("공일공 일이삼사 오육칠팔", "<PHONE_1>"),
    ("영일영일이삼사오육칠팔", "<PHONE_1>"),
    ("010 일이삼사 오육칠팔", "<PHONE_1>"),
    ("0일0 일이삼사 오육칠팔", "<PHONE_1>"),
    ("공일공에 일이삼사에 오육칠팔", "<PHONE_1>"),
    ("공일공 ⟨일이⟩삼사 오육칠팔", "<PHONE_1>"),
    ("구공공일공일 일이삼사오육칠", "<NUMBER_1>"),
    ("영하나영 하나둘삼사오륙칠팔", "<PHONE_1>"),
    ("공하나공 하나둘셋넷 다섯여섯일곱여덟", "<PHONE_1>"),
    ("빵일빵 일이삼사 오륙칠팔", "<PHONE_1>"),
    ("공일공의 일이삼사의 오육칠팔", "<PHONE_1>"),
    ("공일공 다시 일이삼사 다시 오육칠팔", "<PHONE_1>"),
    ("구둘공하나공하나 하나둘삼사오륙칠", "<NUMBER_1>"),
    ("일이삼사오륙칠", "<NUMBER_1>"),
    ("일이삼사 오육칠", "<NUMBER_1>"),
    ("1234567890", "<NUMBER_1>"),
    ("12345678", "<NUMBER_1>"),
    ("1234-5678", "<NUMBER_1>"),
    ("12가3456", "<CAR_1>"),
    ("123호 4567", "<CAR_1>"),
    ("서울특별시 강남구 테헤란로 123", "<ADDRESS_1>"),
    ("서울 강남구 봉은사로68길 12-3", "<ADDRESS_1>"),
    ("강남구 역삼동 123-45번지", "<ADDRESS_1>"),
    ("경기도 성남시 분당구 판교로 256", "<ADDRESS_1>"),
    ("강남구 테헤란로 백이십삼", "<ADDRESS_1>"),
    ("서울 강남구 테헤란로 123, 101동 1203호", "<ADDRESS_1>"),
])
def test_값을_가리고_되돌림(value, token):
    text = f"연락처는 {value}입니다"
    masked, table = mask(text)
    assert masked == f"연락처는 {token}입니다"
    assert value not in masked and table == {token: value}
    assert unmask(masked, table) == text


@pytest.mark.parametrize("text", [
    "[12] 03:21 다음 주에 다시 보자",           # 전사 줄 머리
    "[1] (기획서, 3쪽)\n내용 [2]",              # RAG 조각 머리와 근거 번호
    "회의는 2026-11-05 에 한다",                # 날짜
    "예산은 1,500,000원이고 35만 원 남았다",    # 금액
    "⟨공일⟩ 일이",                              # STT 불확실 표시
    "버전 1.2.3 과 12-34",
    "<자료>\n본문\n</자료>",
    "일이삼사 오육",                            # 7자 미만으로 읽은 번호
    "1234567 은 7자리다",                       # 8자리 미만 숫자
    "12000000원과 3000000 명",                  # 단위가 붙은 수
    "20261105 와 2020-2025",                    # 날짜 모양과 연도 범위
    "뒷번호는 5678",                            # 단서 없는 짧은 숫자는 포기한다
    "생일은 12월 3일",
    "인증번호 6자리와 비밀번호 1234개",
    "발생일 1234",
    "비번 근무",
    "12시 3456",                                # 차량번호에 쓰지 않는 글자
    "하나 둘 셋 넷 시작",                       # 숫자를 세는 말
    "하나 둘 셋 넷 다섯 여섯",
    "다시 이야기해 보자",
    "천이백삼십사",                             # 자릿수로 읽은 수
    "[3] 03:21 일이삼사",                       # 줄 머리의 시간을 번호로 잇지 않는다
    "강남구에서 만나요",                        # 지역만 말한 경우
    "서울 강남구 쪽으로 옮긴다",
    "친구 앞으로 3개 보냈다",                   # 주소 모양과 비슷한 말
    "강남구 테헤란로 3개 지점",
])
def test_개인정보가_아니면_그대로(text):
    assert mask(text) == (text, {})


def test_줄을_넘어서_이어_붙이지_않음():
    text = "[1] 00:01 010\n[2] 00:02 1234\n[3] 00:03 5678"
    assert mask(text) == (text, {})


@pytest.mark.parametrize("text, masked", [
    ("주소는 서울특별시 강남구 테헤란로 123이다", "주소는 <ADDRESS_1>이다"),
    ("주소는 강남구 테헤란로 백이십삼이다", "주소는 <ADDRESS_1>이다"),
    ("번호는 공일공 일이삼사 오육칠팔이에요", "번호는 <PHONE_1>이에요"),
    ("번호는 공일공 일이삼사 오육칠팔이고 메일은 없다", "번호는 <PHONE_1>이고 메일은 없다"),
    ("번호는 공일공 일이삼사 오육칠팔이 맞아요", "번호는 <PHONE_1>이 맞아요"),
    ("번호는 공일공일일일일이이이이고, 주소는 서울 강남구 테헤란로 123이다", "번호는 <PHONE_1>고, 주소는 <ADDRESS_1>이다"),
    ("번호는 공일공 일이삼사 오육칠이에요", "번호는 <PHONE_1>에요"),
    ("번호는 공일공일일일일이이이이이고", "번호는 <PHONE_1>이고"),
    ("뒷번호는 일이삼사오륙칠이고요", "뒷번호는 <NUMBER_1>고요"),
])
def test_값_뒤의_조사_이_는_가리지_않음(text, masked):
    out, table = mask(text)
    assert out == masked and unmask(out, table) == text


@pytest.mark.parametrize("text, masked", [
    ("비밀번호는 1234예요", "비밀번호는 <SECRET_1>예요"),
    ("서버 비번이 일이삼사", "서버 비번이 <SECRET_1>"),
    ("비밀 번호: 482913", "비밀 번호: <SECRET_1>"),
    ("인증번호 482913 입력", "인증번호 <SECRET_1> 입력"),
    ("pin 1234", "pin <SECRET_1>"),
    ("핀번호를 오륙칠팔로 바꿈", "핀번호를 <SECRET_1>로 바꿈"),
    ("비밀번호는 1234-567", "비밀번호는 <SECRET_1>"),
    ("비밀번호는 1234-5678", "비밀번호는 <NUMBER_1>"),
    ("비밀번호는 20261105", "비밀번호는 <SECRET_1>"),
    ("생년월일 900101", "생년월일 <BIRTH_1>"),
    ("생년월일은 1990-01-01", "생년월일은 <BIRTH_1>"),
    ("생년월일 1990년 1월 1일입니다", "생년월일 <BIRTH_1>입니다"),
    ("제 생일은 구공공일공일", "제 생일은 <BIRTH_1>"),
    ("생일 19900101", "생일 <BIRTH_1>"),
])
def test_단서_말은_남기고_뒤의_숫자만_가림(text, masked):
    out, table = mask(text)
    assert out == masked and unmask(out, table) == text


def test_줄_머리_시간_뒤의_번호만_가림():
    assert mask("[3] 03:21 일이삼사오육칠팔")[0] == "[3] 03:21 <NUMBER_1>"


def test_문장_끝_마침표가_있어도_IP_를_가림():
    assert mask("서버는 10.0.0.1.")[0] == "서버는 <IP_1>."


def test_말로_읽은_번호가_두_줄로_나뉘면_못_잡음():
    text = "[1] 00:01 공일공\n[2] 00:02 일이삼사\n[3] 00:03 오육칠팔"
    assert mask(text) == (text, {})


def test_전화번호_두_형식의_번호가_이어짐():
    assert mask("010-1234-5678 공일공 구팔칠육 오사삼이")[0] == "<PHONE_1> <PHONE_2>"


# ---- 번호 ----

def test_같은_값은_같은_번호():
    masked, table = mask("a@example.com b@example.com a@example.com")
    assert masked == "<EMAIL_1> <EMAIL_2> <EMAIL_1>"
    assert len(table) == 2


def test_원문에_대체_표시가_있으면_번호를_건너뜀():
    text = "글자 <PHONE_1> 번호 010-1234-5678"
    masked, table = mask(text)
    assert masked == "글자 <PHONE_1> 번호 <PHONE_2>"
    assert unmask(masked, table) == text


def test_대응표에_없는_표시는_되돌리지_않음():
    assert unmask("<PHONE_9>", {"<PHONE_1>": "010-1234-5678"}) == "<PHONE_9>"


def test_JSON_답도_되돌림():
    masked, table = mask("메일 a@example.com")
    assert unmask('{"text": "<EMAIL_1> 로 보낸다"}', table) == '{"text": "a@example.com 로 보낸다"}'
    assert masked == "메일 <EMAIL_1>"


# ---- 로그 ----

def test_로그에_값을_남기지_않음(caplog):
    caplog.set_level(logging.INFO, logger="shared.masking")
    _, table = mask("a@example.com 010-1234-5678")
    unmask("PHONE_1 만 남음", table)
    assert "EMAIL=1 PHONE=1" in caplog.text and "count=1" in caplog.text
    assert "a@example.com" not in caplog.text and "010-1234-5678" not in caplog.text


# ---- 공용 AI 연결 ----

class Spy:
    name, model = "spy", "m"

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.sent = answer, error, []

    def generate(self, system, user, timeout_s, **kw):
        self.sent.append((system, user, timeout_s, kw))
        if self.error:
            raise self.error
        return Reply(self.answer or user, input_tokens=3, output_tokens=4)


def test_공급자는_가린_글을_받고_답은_되돌림():
    spy = Spy()
    reply = MaskingLLM(spy).generate("시스템", "번호 010-1234-5678", 9.0)
    assert spy.sent == [("시스템", "번호 <PHONE_1>", 9.0, {})]
    assert reply == Reply("번호 010-1234-5678", input_tokens=3, output_tokens=4)


def test_effort_는_있을_때만_넘김():
    spy = Spy()
    MaskingLLM(spy).generate("s", "u", 1.0, effort="low")
    assert spy.sent[0][3] == {"effort": "low"}


def test_가리다_오류가_나면_보내지_않음(monkeypatch):
    def broken(_):
        raise RuntimeError("010-1234-5678")
    monkeypatch.setattr("shared.llm.mask", broken)
    spy = Spy()
    with pytest.raises(LLMError) as e:
        MaskingLLM(spy).generate("s", "010-1234-5678", 1.0)
    assert spy.sent == [] and "010-1234-5678" not in str(e.value)


def test_공급자_예외는_그대로_나감():
    with pytest.raises(LLMError, match="끊김"):
        MaskingLLM(Spy(error=LLMError("끊김"))).generate("s", "u", 1.0)


def test_폴백으로_넘어가도_가린_글만_보냄():
    primary, fallback = Spy(error=LLMError("x")), Spy()
    llm = FallbackLLM(MaskingLLM(primary), MaskingLLM(fallback))
    reply = llm.generate("s", "번호 010-1234-5678", 1.0)
    assert primary.sent[0][1] == fallback.sent[0][1] == "번호 <PHONE_1>"
    assert reply.text == "번호 010-1234-5678" and reply.fallback


def test_실제_공급자는_기본과_폴백_모두_감쌈():
    pytest.importorskip("openai")
    env = {**OPENAI_ENV, "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "b"}
    llm, _ = make_llm(env, "RAG_LLM")
    wrapped = add_fallback(llm, env, "RAG_LLM")
    assert isinstance(wrapped.primary, MaskingLLM) and isinstance(wrapped.fallback, MaskingLLM)


def test_시험용_가짜_AI_는_감싸지_않음():
    llm, _ = make_llm({}, "RAG_LLM")
    assert not isinstance(llm, MaskingLLM)


# ---- 요약 호출 함수용 ----

def test_masked_call_은_user_만_가리고_답을_되돌림():
    seen = {}

    @masked_call
    def call(provider, model, system, user, mock_file=None, task=None):
        seen.update(system=system, user=user, task=task)
        return f'{{"text": "{user}"}}'

    out = call("openai", "m", "시스템 a@example.com", "메일 a@example.com", task="draft")
    assert seen == {"system": "시스템 a@example.com", "user": "메일 <EMAIL_1>", "task": "draft"}
    assert out == '{"text": "메일 a@example.com"}'


def test_masked_call_은_가리다_오류가_나면_부르지_않음(monkeypatch):
    def broken(_):
        raise RuntimeError("x")
    monkeypatch.setattr(masking, "mask", broken)
    calls = []
    wrapped = masked_call(lambda *a: calls.append(a))
    with pytest.raises(RuntimeError):
        wrapped("p", "m", "s", "u")
    assert calls == []
