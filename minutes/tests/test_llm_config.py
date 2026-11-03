"""초안 AI 설정 고르기 시험. 실제 AI 는 부르지 않는다 (fake 이거나 설정 오류에서 멈춤)."""
import pytest

from minutes.llm_config import MINUTES, RAG, make_minutes_llm
from shared.llm import LLMConfigError

KEY = {"OPENAI_API_KEY": "k" * 20}          # 클라이언트만 만들고 부르지는 않는다


def test_초안_설정이_없으면_RAG_설정을_그대로_쓴다():
    pytest.importorskip("openai")
    llm, _, source = make_minutes_llm({**KEY, "RAG_LLM_PROVIDER": "openai", "RAG_LLM_MODEL": "rag-model"})
    assert source == RAG
    assert (llm.name, llm.model) == ("openai", "rag-model")


def test_초안_공급자가_있으면_공급자와_모델을_한_묶음으로_쓴다():
    pytest.importorskip("openai")
    env = {**KEY, "RAG_LLM_PROVIDER": "gemini", "RAG_LLM_MODEL": "rag-model", "MINUTES_LLM_PROVIDER": "openai"}
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_MODEL 이 필요함"):    # RAG 모델이 섞이지 않는다
        make_minutes_llm(env)
    llm, _, source = make_minutes_llm({**env, "MINUTES_LLM_MODEL": "minutes-model"})
    assert source == MINUTES
    assert (llm.name, llm.model) == ("openai", "minutes-model")


def test_초안_모델만_있으면_설정_오류():
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_PROVIDER 와 함께"):
        make_minutes_llm({"RAG_LLM_PROVIDER": "gemini", "MINUTES_LLM_MODEL": "m"})


def test_시간_제한은_어느_쪽이든_초안_설정을_쓴다():
    _, timeout_s, _ = make_minutes_llm({"RAG_LLM_TIMEOUT_S": "5", "MINUTES_LLM_TIMEOUT_S": "42"})
    assert timeout_s == 42.0
    _, timeout_s, source = make_minutes_llm({"RAG_LLM_TIMEOUT_S": "5"})
    assert (timeout_s, source) == (60.0, RAG)


def test_원래_RAG_설정은_바꾸지_않는다():
    env = {"RAG_LLM_PROVIDER": "fake", "MINUTES_LLM_PROVIDER": "fake"}
    make_minutes_llm(env)
    assert env == {"RAG_LLM_PROVIDER": "fake", "MINUTES_LLM_PROVIDER": "fake"}


@pytest.mark.parametrize("env, word", [
    ({"MINUTES_LLM_PROVIDER": "gemini"}, "MINUTES_LLM_MODEL 이 필요함"),
    ({"MINUTES_LLM_PROVIDER": "nope"}, "MINUTES_LLM_PROVIDER"),
    ({"RAG_LLM_PROVIDER": "gemini"}, "RAG_LLM_MODEL 이 필요함"),
    ({"MINUTES_LLM_TIMEOUT_S": "x"}, "MINUTES_LLM_TIMEOUT_S 는 숫자"),
])
def test_오류_메시지는_고른_쪽_설정_이름으로(env, word):
    with pytest.raises(LLMConfigError, match=word):
        make_minutes_llm(env)


# ---- 폴백 모델 (서버만) ----

def test_폴백은_켤_때만_붙임():
    from shared.llm import FallbackLLM
    env = {"RAG_LLM_FALLBACK_PROVIDER": "gemini", "RAG_LLM_FALLBACK_MODEL": "m", "GEMINI_API_KEY": "k" * 20}
    llm, _, _ = make_minutes_llm(env)                      # 평가·손 시험 도구는 fallback 을 넘기지 않는다
    assert not isinstance(llm, FallbackLLM)
    pytest.importorskip("google.genai")
    llm, _, _ = make_minutes_llm(env, fallback=True)
    assert isinstance(llm, FallbackLLM) and llm.fallback.model == "m"


@pytest.mark.parametrize("env, expected", [
    ({"RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "rag-spare"}, "openai/rag-spare"),
    ({"RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "rag-spare",
      "MINUTES_LLM_FALLBACK_PROVIDER": "openai", "MINUTES_LLM_FALLBACK_MODEL": "minutes-spare"}, "openai/minutes-spare"),
    # 기본 모델은 회의록 설정, 폴백은 RAG 설정 (따로 고른다)
    ({"MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "minutes-main",
      "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "rag-spare"}, "openai/rag-spare"),
])
def test_회의록_폴백_설정이_없으면_RAG_폴백을_따름(env, expected):
    from shared.llm import label
    pytest.importorskip("openai")
    llm, _, _ = make_minutes_llm({**KEY, **env}, fallback=True)
    assert label(llm.fallback) == expected


def test_회의록_폴백_모델만_있으면_설정_오류():
    with pytest.raises(LLMConfigError, match="MINUTES_LLM_FALLBACK_MODEL 는 MINUTES_LLM_FALLBACK_PROVIDER 와 함께"):
        make_minutes_llm({"RAG_LLM_FALLBACK_PROVIDER": "fake", "MINUTES_LLM_FALLBACK_MODEL": "m"}, fallback=True)


def test_RAG_폴백이_회의록_기본_모델과_같으면_폴백_없이():
    from shared.llm import FallbackLLM
    pytest.importorskip("openai")
    env = {**KEY, "MINUTES_LLM_PROVIDER": "openai", "MINUTES_LLM_MODEL": "gpt-5.6-luna",
           "RAG_LLM_FALLBACK_PROVIDER": "openai", "RAG_LLM_FALLBACK_MODEL": "gpt-5.6-luna"}
    llm, _, _ = make_minutes_llm(env, fallback=True)
    assert not isinstance(llm, FallbackLLM) and llm.model == "gpt-5.6-luna"
