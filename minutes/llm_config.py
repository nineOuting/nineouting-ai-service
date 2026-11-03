"""회의록 초안이 쓰는 AI 설정. AI 연결 코드는 RAG 답변과 같이 쓰고(shared/llm.py), 설정만 따로 고를 수 있게 한다.

  MINUTES_LLM_PROVIDER 가 있으면   MINUTES_LLM_PROVIDER, MINUTES_LLM_MODEL
  없으면                          RAG 답변 설정 RAG_LLM_PROVIDER, RAG_LLM_MODEL 을 그대로

공급자와 모델은 한 묶음으로 고른다 (한쪽 공급자에 다른 쪽 모델이 섞이지 않게).
API 키(GEMINI_API_KEY 등)는 같이 쓴다. 시간 제한은 어느 쪽이든 MINUTES_LLM_TIMEOUT_S (기본 60초).
"""
from shared.llm import LLMConfigError, make_llm

DEFAULT_TIMEOUT_S = 60.0
MINUTES, RAG = "MINUTES_LLM", "RAG_LLM"
TIMEOUT_KEY = "MINUTES_LLM_TIMEOUT_S"


def llm_source(env) -> str:
    """공급자·모델을 읽을 쪽(MINUTES 또는 RAG)을 고른다."""
    if env.get("MINUTES_LLM_PROVIDER"):
        return MINUTES
    if env.get("MINUTES_LLM_MODEL"):
        raise LLMConfigError("MINUTES_LLM_MODEL 은 MINUTES_LLM_PROVIDER 와 함께 넣어야 함")
    return RAG


def make_minutes_llm(env):
    """(llm, 시간 제한 초, 고른 쪽). 설정이 틀리면 LLMConfigError."""
    source = llm_source(env)
    llm, timeout_s = make_llm(env, source, timeout_key=TIMEOUT_KEY, default_timeout_s=DEFAULT_TIMEOUT_S)
    return llm, timeout_s, source
