"""회의록 초안이 쓰는 AI 설정. AI 연결 코드는 RAG 답변과 같이 쓰고(shared/llm.py), 설정만 따로 고를 수 있게 한다.

  MINUTES_LLM_PROVIDER 가 있으면   MINUTES_LLM_PROVIDER, MINUTES_LLM_MODEL
  없으면                          RAG 답변 설정 RAG_LLM_PROVIDER, RAG_LLM_MODEL 을 그대로

폴백 모델(서버에서만)도 같은 방식으로 따로 고른다. 기본 모델을 어느 쪽에서 골랐는지와 상관없다.
  MINUTES_LLM_FALLBACK_PROVIDER 나 _MODEL 이 있으면   MINUTES_LLM_FALLBACK_PROVIDER·_MODEL·_COOLDOWN_S
  없으면                                             RAG_LLM_FALLBACK_PROVIDER·_MODEL·_COOLDOWN_S 를 그대로

공급자와 모델은 한 묶음으로 고른다 (한쪽 공급자에 다른 쪽 모델이 섞이지 않게).
API 키(GEMINI_API_KEY 등)는 같이 쓴다. 시간 제한은 어느 쪽이든 MINUTES_LLM_TIMEOUT_S (기본 60초).
"""
from shared.llm import LLMConfigError, add_fallback, make_llm

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


def fallback_source(env) -> str:
    """폴백 설정을 읽을 쪽(MINUTES 또는 RAG)을 고른다. 회의록 쪽에 공급자나 모델이 하나라도 있으면 회의록 쪽
    (모델만 있으면 add_fallback 이 설정 오류를 낸다)."""
    if env.get("MINUTES_LLM_FALLBACK_PROVIDER") or env.get("MINUTES_LLM_FALLBACK_MODEL"):
        return MINUTES
    return RAG


def make_minutes_llm(env, fallback: bool = False):
    """(llm, 시간 제한 초, 고른 쪽). 설정이 틀리면 LLMConfigError.
    fallback=True 면 폴백 모델을 붙인다. 서버(runtime.py)와 따로 띄운 작업자(worker.py)만 켠다."""
    source = llm_source(env)
    llm, timeout_s = make_llm(env, source, timeout_key=TIMEOUT_KEY, default_timeout_s=DEFAULT_TIMEOUT_S)
    if fallback:
        llm = add_fallback(llm, env, fallback_source(env))
    return llm, timeout_s, source
