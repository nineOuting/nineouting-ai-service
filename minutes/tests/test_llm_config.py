"""초안 AI 설정 시험. 실제 AI 는 부르지 않는다 (fake 이거나 설정 오류에서 멈춤)."""
import pytest

from minutes.llm_config import make_minutes_llm
from shared.llm import FallbackLLM, LLMConfigError, label

KEY = {"OPENAI_API_KEY": "k" * 20}          # 클라이언트만 만들고 부르지는 않는다
RAG_SET = {"RAG_LLM_PROVIDER": "openai", "RAG_LLM_MODEL": "rag-model",
           "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "rag-spare"}


def test_초안_설정으로_공급자와_모델을_고른다():
    pytest.importorskip("openai")
    llm, timeout_s = make_minutes_llm({**KEY, "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "minutes-model"})
    assert (label(llm), timeout_s) == ("openai/minutes-model", 60.0)


def test_초안_설정이_없으면_RAG_설정이_있어도_오류():
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_PROVIDER 가 없음 .*RAG 설정을 따르지 않는다"):
        make_minutes_llm({**KEY, **RAG_SET})
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_PROVIDER 가 없음"):
        make_minutes_llm({})


def test_가짜_AI_는_직접_적었을_때만():
    llm, _ = make_minutes_llm({"MINUTES_LLM_PROVIDER": "fake"})
    assert label(llm) == "fake/fake"


def test_RAG_설정은_섞이지_않는다():
    """RAG 모델·시간 제한·폴백이 회의록 초안에 들어오지 않는다."""
    pytest.importorskip("openai")
    env = {**KEY, **RAG_SET, "RAG_LLM_TIMEOUT_S": "5", "MINUTES_LLM_PROVIDER": "openai"}
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_MODEL 이 필요함"):
        make_minutes_llm(env)
    llm, timeout_s = make_minutes_llm({**env, "MINUTES_LLM_MODEL": "minutes-model"}, fallback=True)
    assert (label(llm), timeout_s) == ("openai/minutes-model", 60.0)        # 폴백도 RAG 것을 붙이지 않음


def test_초안_모델만_있으면_설정_오류():
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_MODEL 은 MINUTES_LLM_PROVIDER 와 함께"):
        make_minutes_llm({"RAG_LLM_PROVIDER": "fake", "MINUTES_LLM_MODEL": "m"})


def test_시간_제한은_초안_설정():
    _, timeout_s = make_minutes_llm({"MINUTES_LLM_PROVIDER": "fake", "MINUTES_LLM_TIMEOUT_S": "42"})
    assert timeout_s == 42.0


def test_넘긴_설정은_바꾸지_않는다():
    env = {"RAG_LLM_PROVIDER": "fake", "MINUTES_LLM_PROVIDER": "fake"}
    make_minutes_llm(env, fallback=True)
    assert env == {"RAG_LLM_PROVIDER": "fake", "MINUTES_LLM_PROVIDER": "fake"}


@pytest.mark.parametrize("env, word", [
    ({"MINUTES_LLM_PROVIDER": "gemini"}, "MINUTES_LLM_MODEL 이 필요함"),
    ({"MINUTES_LLM_PROVIDER": "nope"}, "MINUTES_LLM_PROVIDER"),
    ({"MINUTES_LLM_PROVIDER": "fake", "MINUTES_LLM_TIMEOUT_S": "x"}, "MINUTES_LLM_TIMEOUT_S 는 숫자"),
])
def test_오류_메시지는_초안_설정_이름으로(env, word):
    with pytest.raises(LLMConfigError, match=word):
        make_minutes_llm(env)


# ---- 폴백 모델 (서버만) ----

def test_폴백은_켤_때만_붙임():
    env = {"MINUTES_LLM_PROVIDER": "fake", "MINUTES_LLM_FALLBACK_PROVIDER": "gemini",
           "MINUTES_LLM_FALLBACK_MODEL": "m", "GEMINI_API_KEY": "k" * 20}
    llm, _ = make_minutes_llm(env)                         # 평가·손 시험 도구는 fallback 을 넘기지 않는다
    assert not isinstance(llm, FallbackLLM)
    pytest.importorskip("google.genai")
    llm, _ = make_minutes_llm(env, fallback=True)
    assert isinstance(llm, FallbackLLM) and label(llm.fallback) == "gemini/m"


def test_회의록_폴백_설정이_없으면_RAG_폴백이_있어도_폴백_없음():
    pytest.importorskip("openai")
    env = {**KEY, **RAG_SET, "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "minutes-main"}
    llm, _ = make_minutes_llm(env, fallback=True)
    assert not isinstance(llm, FallbackLLM) and label(llm) == "openai/minutes-main"


def test_회의록_폴백_설정을_읽음():
    pytest.importorskip("openai")
    env = {**KEY, **RAG_SET, "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "minutes-main",
           "MINUTES_LLM_FALLBACK_PROVIDER": "openai", "MINUTES_LLM_FALLBACK_MODEL": "minutes-spare",
           "MINUTES_LLM_FALLBACK_COOLDOWN_S": "15"}
    llm, _ = make_minutes_llm(env, fallback=True)
    assert (label(llm.fallback), llm.cooldown_s) == ("openai/minutes-spare", 15.0)


def test_회의록_폴백_모델만_있으면_설정_오류():
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_FALLBACK_MODEL 는 MINUTES_LLM_FALLBACK_PROVIDER 와 함께"):
        make_minutes_llm({"MINUTES_LLM_PROVIDER": "fake", "RAG_LLM_FALLBACK_PROVIDER": "fake",
                          "MINUTES_LLM_FALLBACK_MODEL": "m"}, fallback=True)


def test_폴백이_기본_모델과_같으면_폴백_없이():
    pytest.importorskip("openai")
    env = {**KEY, "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "gpt-5.6-luna",
           "MINUTES_LLM_FALLBACK_PROVIDER": "openai", "MINUTES_LLM_FALLBACK_MODEL": "gpt-5.6-luna"}
    llm, _ = make_minutes_llm(env, fallback=True)
    assert not isinstance(llm, FallbackLLM) and llm.model == "gpt-5.6-luna"
