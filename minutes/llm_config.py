"""회의록 초안이 쓰는 AI 설정. AI 연결 코드는 RAG 답변과 같이 쓰고(shared/llm.py), 설정은 회의록 초안 것만 읽는다.

  MINUTES_LLM_PROVIDER, MINUTES_LLM_MODEL          꼭 넣는다. 없으면 설정 오류 (서버는 뜨고 초안 주소만 503)
  MINUTES_LLM_TIMEOUT_S                            시간 제한 (기본 60초)
  MINUTES_LLM_FALLBACK_PROVIDER·_MODEL·_COOLDOWN_S  폴백 모델 (선택, 서버에서만). 없으면 폴백 없이 쓴다

RAG 답변 설정(RAG_LLM_*)은 보지 않는다. 시험용 가짜 AI 는 MINUTES_LLM_PROVIDER=fake 로 직접 적었을 때만 쓴다.
API 키(OPENAI_API_KEY 등)는 RAG 답변과 같이 쓴다.
"""
from shared.llm import LLMConfigError, add_fallback, make_llm

DEFAULT_TIMEOUT_S = 60.0
PREFIX = "MINUTES_LLM"
TIMEOUT_KEY = "MINUTES_LLM_TIMEOUT_S"


def make_minutes_llm(env, fallback: bool = False):
    """(llm, 시간 제한 초). 설정이 없거나 틀리면 LLMConfigError.
    fallback=True 면 폴백 모델을 붙인다. 서버(runtime.py)와 따로 띄운 작업자(worker.py)만 켠다."""
    if not env.get("MINUTES_LLM_PROVIDER"):
        if env.get("MINUTES_LLM_MODEL"):
            raise LLMConfigError("MINUTES_LLM_MODEL 은 MINUTES_LLM_PROVIDER 와 함께 넣어야 함")
        raise LLMConfigError("MINUTES_LLM_PROVIDER 가 없음 (회의록 초안은 RAG 설정을 따르지 않는다. 시험용이면 fake)")
    llm, timeout_s = make_llm(env, PREFIX, timeout_key=TIMEOUT_KEY, default_timeout_s=DEFAULT_TIMEOUT_S)
    if fallback:
        llm = add_fallback(llm, env, PREFIX)
    return llm, timeout_s
