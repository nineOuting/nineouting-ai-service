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
