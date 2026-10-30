"""회의록 초안이 쓰는 AI 설정. AI 연결 코드는 RAG 답변과 같이 쓰고(rag/llm.py), 설정만 따로 고를 수 있게 한다.

  MINUTES_LLM_PROVIDER 가 있으면   MINUTES_LLM_PROVIDER, MINUTES_LLM_MODEL
  없으면                          RAG 답변 설정 RAG_LLM_PROVIDER, RAG_LLM_MODEL 을 그대로

공급자와 모델은 한 묶음으로 고른다 (한쪽 공급자에 다른 쪽 모델이 섞이지 않게).
API 키(GEMINI_API_KEY 등)는 같이 쓴다. 시간 제한은 어느 쪽이든 MINUTES_LLM_TIMEOUT_S (기본 60초).
"""
from rag.llm import LLMConfigError, make_llm

DEFAULT_TIMEOUT_S = 60.0
MINUTES, RAG = "MINUTES_LLM", "RAG_LLM"


def llm_env(env) -> tuple[dict, str]:
    """make_llm 에 넘길 설정과 고른 쪽(MINUTES 또는 RAG)을 돌려준다."""
    timeout = env.get("MINUTES_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    if env.get("MINUTES_LLM_PROVIDER"):
        return {**env, "RAG_LLM_PROVIDER": env["MINUTES_LLM_PROVIDER"],
                "RAG_LLM_MODEL": env.get("MINUTES_LLM_MODEL", ""), "RAG_LLM_TIMEOUT_S": timeout}, MINUTES
    if env.get("MINUTES_LLM_MODEL"):
        raise LLMConfigError("MINUTES_LLM_MODEL 은 MINUTES_LLM_PROVIDER 와 함께 넣어야 함")
    return {**env, "RAG_LLM_TIMEOUT_S": timeout}, RAG


def make_minutes_llm(env):
    """(llm, 시간 제한 초, 고른 쪽). 설정이 틀리면 LLMConfigError. 메시지의 설정 이름은 고른 쪽 이름으로 바꾼다."""
    mapped, source = llm_env(env)
    try:
        llm, timeout_s = make_llm(mapped)
    except LLMConfigError as e:
        msg = str(e).replace("RAG_LLM_TIMEOUT_S", "MINUTES_LLM_TIMEOUT_S")
        if source == MINUTES:
            msg = msg.replace("RAG_LLM_", "MINUTES_LLM_")
        raise LLMConfigError(msg) from None
    return llm, timeout_s, source
