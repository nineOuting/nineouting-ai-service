"""답변용 LLM 연결. 공급자를 바꿔 끼울 수 있게 generate() 하나만 둔다.

지금은 가짜(fake)만 있다. 실제 공급자(운영 OpenAI, 시험 Gemini)는 SDK 를 공용 requirements.txt 에
넣는 방식을 정한 뒤 붙인다. 공급자 SDK 의 예외는 LLMError·LLMTimeout 으로 바꿔서 던진다.

  RAG_LLM_PROVIDER   fake (기본)
  RAG_LLM_MODEL      공급자의 모델 이름 (가짜는 쓰지 않음)
  RAG_LLM_TIMEOUT_S  응답을 기다리는 상한 (기본 30초)
"""
from typing import Protocol

PROVIDERS = ("fake",)
DEFAULT_TIMEOUT_S = 30.0


class LLMError(Exception):
    """호출 실패. 답변은 failed / llm_failed"""


class LLMTimeout(LLMError):
    """시간 초과. 답변은 failed / llm_timeout"""


class LLMConfigError(ValueError):
    """공급자 설정이 잘못됨. 서버는 그대로 뜨고 답변 API 만 503 으로 답한다"""


class LLM(Protocol):
    name: str

    def generate(self, system: str, user: str, timeout_s: float) -> str: ...


class FakeLLM:
    """시험용. 근거 1번이 있으면 정해진 문장에 [1] 을 붙여 돌려주고, 없으면 NO_ANSWER.
    배포된 서버에서도 기본값이라, 실제 답으로 착각하지 않게 앞에 표시를 붙인다. 조각 글은 되풀이하지 않는다."""
    name = "fake"
    MARK = "[시험용 답변]"

    def generate(self, system, user, timeout_s):
        if "[1] " not in user:
            return "NO_ANSWER"
        return f"{self.MARK} 실제 AI 를 연결하기 전의 시험용 답입니다. 근거 1번을 참고했습니다 [1]."


def make_llm(env) -> tuple[LLM, float]:
    """환경변수로 LLM 과 시간 제한을 만든다. 설정이 잘못되면 LLMConfigError."""
    provider = env.get("RAG_LLM_PROVIDER", "fake")
    if provider not in PROVIDERS:
        raise LLMConfigError(f"지원하지 않는 RAG_LLM_PROVIDER: {provider} (가능: {', '.join(PROVIDERS)})")
    raw = env.get("RAG_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    try:
        timeout_s = float(raw)
    except (TypeError, ValueError):
        raise LLMConfigError("RAG_LLM_TIMEOUT_S 는 숫자여야 함") from None
    if timeout_s <= 0:
        raise LLMConfigError("RAG_LLM_TIMEOUT_S 는 0보다 커야 함")
    return FakeLLM(), timeout_s
