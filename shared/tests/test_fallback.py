"""폴백 모델 시험. 실제 AI 는 부르지 않는다 (정해 둔 답이나 예외를 내는 가짜를 끼운다). 시계도 바꿔 끼운다."""
import logging
import threading

import pytest

from shared.llm import (FakeLLM, FallbackLLM, LLMConfigError, LLMError, LLMTimeout, Reply, add_fallback, describe,
                        label)


class Stub:
    """정해 둔 결과를 차례로 낸다. 결과가 예외면 던진다. 다 쓰면 마지막 결과를 계속 낸다."""

    def __init__(self, name, model, *results):
        self.name, self.model, self.results, self.calls = name, model, list(results), 0

    def generate(self, system, user, timeout_s):
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


OK = Reply("답 [1].", 10, 5)


def pair(primary_results, fallback_results=(OK,), cooldown_s=60):
    clock = Clock()
    primary = Stub("openai", "main", *primary_results)
    fallback = Stub("openai", "spare", *fallback_results)
    return FallbackLLM(primary, fallback, cooldown_s, clock=clock), primary, fallback, clock


# ---- 흐름 ----

def test_기본_모델이_답하면_폴백을_부르지_않음():
    llm, primary, fallback, _ = pair([OK])
    r = llm.generate("s", "u", 5)
    assert (r.text, r.model, r.fallback) == ("답 [1].", "openai/main", False)
    assert (primary.calls, fallback.calls) == (1, 0)


@pytest.mark.parametrize("error", [LLMError("RateLimitError"), LLMTimeout("APITimeoutError"), LLMError("빈 응답")])
def test_기본_모델_호출이_실패하면_폴백이_답함(error, caplog):
    llm, primary, fallback, _ = pair([error])
    with caplog.at_level(logging.WARNING, logger="shared.llm"):
        r = llm.generate("s", "u", 5)
    assert (r.text, r.input_tokens, r.model, r.fallback) == ("답 [1].", 10, "openai/spare", True)
    assert (primary.calls, fallback.calls) == (1, 1)
    assert f"primary=openai/main fallback=openai/spare error={error} cooldown_s=60" in caplog.text


def test_폴백은_같은_입력과_시간_제한으로_부름():
    seen = []

    class Spy(Stub):
        def generate(self, system, user, timeout_s):
            seen.append((system, user, timeout_s))
            return OK

    llm = FallbackLLM(Stub("openai", "main", LLMError("x")), Spy("openai", "spare", OK))
    llm.generate("지시문", "입력", 7.5)
    assert seen == [("지시문", "입력", 7.5)]


def test_생각하는_양은_줄_때만_두_모델에_넘김():
    seen = []

    class Spy(Stub):
        def generate(self, system, user, timeout_s, **kw):
            seen.append(kw)
            if self.model == "main":
                raise LLMError("x")
            return OK

    llm = FallbackLLM(Spy("openai", "main", OK), Spy("openai", "spare", OK))
    llm.generate("s", "u", 1, effort="low")
    llm.generate("s", "u", 1)                    # 건너뛰는 동안이라 폴백만
    assert seen == [{"effort": "low"}, {"effort": "low"}, {}]


def test_건너뛰는_동안은_기본_모델을_부르지_않고_시간이_지나면_다시_부름(caplog):
    llm, primary, fallback, clock = pair([LLMError("x"), OK])
    llm.generate("s", "u", 5)
    clock.now += 59
    assert llm.generate("s", "u", 5).fallback
    assert (primary.calls, fallback.calls) == (1, 2)
    clock.now += 2                                          # 실패 뒤 61초
    with caplog.at_level(logging.INFO, logger="shared.llm"):
        r = llm.generate("s", "u", 5)
    assert (r.model, r.fallback) == ("openai/main", False)
    assert (primary.calls, fallback.calls) == (2, 2)
    assert "기본 모델 다시 정상: primary=openai/main" in caplog.text


def test_다시_부른_기본_모델이_또_실패하면_다시_건너뜀():
    llm, primary, fallback, clock = pair([LLMError("x"), LLMError("y"), OK])
    llm.generate("s", "u", 5)
    clock.now += 61
    assert llm.generate("s", "u", 5).fallback               # 다시 시험 → 또 실패 → 폴백
    clock.now += 30
    assert llm.generate("s", "u", 5).fallback               # 두 번째 실패에서 다시 60초
    assert (primary.calls, fallback.calls) == (2, 3)


def test_건너뛰는_시간이_0이면_매번_기본_모델부터():
    llm, primary, fallback, _ = pair([LLMError("x")], cooldown_s=0)
    llm.generate("s", "u", 5)
    llm.generate("s", "u", 5)
    assert (primary.calls, fallback.calls) == (2, 2)


@pytest.mark.parametrize("error, cls", [(LLMTimeout("APITimeoutError"), LLMTimeout), (LLMError("빈 응답"), LLMError)])
def test_둘_다_실패하면_폴백의_예외가_나감(error, cls):
    llm, _, _, _ = pair([LLMError("x")], [error])
    with pytest.raises(cls) as e:
        llm.generate("s", "u", 5)
    assert e.value is error


def test_LLMError_가_아닌_예외는_폴백하지_않음():
    """공급자 예외는 LLM 클래스가 모두 LLMError 로 바꾼다. 그 밖의 예외는 코드 오류라 숨기지 않는다."""
    llm, _, fallback, _ = pair([ValueError("버그")])
    with pytest.raises(ValueError):
        llm.generate("s", "u", 5)
    assert fallback.calls == 0


def test_여러_스레드가_동시에_불러도_건너뛰는_동안_기본_모델을_부르지_않음():
    llm, primary, fallback, _ = pair([LLMError("x")])
    llm.generate("s", "u", 5)
    replies = []
    threads = [threading.Thread(target=lambda: replies.append(llm.generate("s", "u", 5))) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(replies) == 20 and all(r.fallback for r in replies)
    assert (primary.calls, fallback.calls) == (1, 21)


def test_이름은_기본_모델_시작_로그는_둘_다():
    llm, _, _, _ = pair([OK])
    assert label(llm) == "openai/main"
    assert describe(llm) == "openai/main, 폴백 openai/spare (건너뛰기 60초)"
    assert describe(FakeLLM()) == "fake/fake"


# ---- 설정 (add_fallback) ----

OPENAI_KEY = {"OPENAI_API_KEY": "k" * 20}          # 클라이언트만 만들고 부르지는 않는다


def test_폴백_설정이_없으면_그대로():
    base = FakeLLM()
    assert add_fallback(base, {}, "RAG_LLM") is base


def test_폴백_설정이_있으면_감싸고_건너뛰는_시간을_읽음():
    pytest.importorskip("openai")
    env = {**OPENAI_KEY, "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "spare",
           "RAG_LLM_FALLBACK_COOLDOWN_S": "15"}
    llm = add_fallback(FakeLLM(), env, "RAG_LLM")
    assert isinstance(llm, FallbackLLM)
    assert (label(llm.primary), label(llm.fallback), llm.cooldown_s) == ("fake/fake", "openai/spare", 15.0)


def test_건너뛰는_시간_기본값은_60초():
    pytest.importorskip("openai")
    env = {**OPENAI_KEY, "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "spare"}
    assert add_fallback(FakeLLM(), env, "RAG_LLM").cooldown_s == 60.0


def test_넘긴_앞부분의_폴백_설정만_읽음():
    pytest.importorskip("openai")
    env = {**OPENAI_KEY, "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "rag-spare",
           "MINUTES_LLM_FALLBACK_PROVIDER": "openai", "MINUTES_LLM_FALLBACK_MODEL": "minutes-spare"}
    assert label(add_fallback(FakeLLM(), env, "MINUTES_LLM").fallback) == "openai/minutes-spare"


def test_폴백이_기본_모델과_같으면_감싸지_않음(caplog):
    base = FakeLLM()
    with caplog.at_level(logging.INFO, logger="shared.llm"):
        assert add_fallback(base, {"RAG_LLM_FALLBACK_PROVIDER": "fake"}, "RAG_LLM") is base
    assert "폴백 모델이 기본 모델과 같아 폴백 없이 씀: fake/fake" in caplog.text


@pytest.mark.parametrize("env, message", [
    ({"RAG_LLM_FALLBACK_MODEL": "spare"}, "RAG_LLM_FALLBACK_MODEL 는 RAG_LLM_FALLBACK_PROVIDER 와 함께 넣어야 함"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "openai"}, "RAG_LLM_FALLBACK_PROVIDER=openai 에는 RAG_LLM_FALLBACK_MODEL 이 필요함"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "anthropic", "RAG_LLM_FALLBACK_MODEL": "m"}, "지원하지 않는 RAG_LLM_FALLBACK_PROVIDER"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "m"}, "API 키가 없음 (OPENAI_API_KEY)"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "fake", "RAG_LLM_FALLBACK_COOLDOWN_S": "x"}, "RAG_LLM_FALLBACK_COOLDOWN_S 는 숫자"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "fake", "RAG_LLM_FALLBACK_COOLDOWN_S": "-1"}, "0 이상"),
])
def test_폴백_설정_오류(env, message):
    with pytest.raises(LLMConfigError) as e:
        add_fallback(Stub("openai", "main", OK), env, "RAG_LLM")
    assert message in str(e.value)


def test_폴백_설정_오류_메시지에_키_값이_없음(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "openai", None)        # SDK 가 없는 상태
    secret = "sk-비밀키-값"
    with pytest.raises(LLMConfigError) as e:
        add_fallback(FakeLLM(), {"RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "m",
                                 "OPENAI_API_KEY": secret}, "RAG_LLM")
    assert "SDK(openai)" in str(e.value) and secret not in str(e.value)
