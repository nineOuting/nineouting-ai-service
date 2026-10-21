"""답변용 LLM 연결. 공급자를 바꿔 끼울 수 있게 generate() 하나만 둔다.

  RAG_LLM_PROVIDER   fake (기본, 시험용 가짜) / gemini (시험) / openai (운영)
  RAG_LLM_MODEL      공급자의 모델 이름. gemini·openai 는 꼭 넣는다 (기본값 없음)
  RAG_LLM_TIMEOUT_S  응답을 기다리는 상한 (기본 30초). 재시도는 하지 않는다 (기다림이 길어지지 않게)
  API 키             GEMINI_API_KEY(또는 GOOGLE_API_KEY) / OPENAI_API_KEY. 요약 서버와 같은 이름

모델·키·SDK 가 없으면 LLMConfigError 를 낸다. 서버는 그대로 뜨고 답변 API 만 503 이다.
공급자 SDK 의 예외는 LLMTimeout(시간 초과)·LLMError(그 밖) 로 바꿔서 던진다. 메시지에는 예외 이름만 넣는다.
SDK: gemini 는 공용 requirements.txt 의 google-genai. openai 는 운영 모델을 정할 때 넣는다.
"""
import re
from dataclasses import dataclass
from typing import Protocol

PROVIDERS = ("fake", "gemini", "openai")
DEFAULT_TIMEOUT_S = 30.0
_KEYS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "openai": ("OPENAI_API_KEY",)}
_TIMEOUT_ERRORS = {"TimeoutException", "ReadTimeout", "ConnectTimeout", "APITimeoutError", "TimeoutError"}


class LLMError(Exception):
    """호출 실패. 답변은 failed / llm_failed"""


class LLMTimeout(LLMError):
    """시간 초과. 답변은 failed / llm_timeout"""


class LLMConfigError(ValueError):
    """공급자 설정이 잘못됨. 서버는 그대로 뜨고 답변 API 만 503 으로 답한다"""


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None          # 답 토큰. 추론 토큰이 따로 있으면 더한 값 (출력 요금 기준)


class LLM(Protocol):
    name: str
    model: str

    def generate(self, system: str, user: str, timeout_s: float) -> Reply: ...


def _wrap(e):
    """공급자 예외 → LLMTimeout / LLMError. 예외 메시지에 요청 내용이 섞일 수 있어 이름만 쓴다."""
    names = {c.__name__ for c in type(e).__mro__}
    cls = LLMTimeout if names & _TIMEOUT_ERRORS else LLMError
    return cls(type(e).__name__)


class FakeLLM:
    """시험용. 넘겨받은 조각 번호를 모두 근거로 단 정해진 문장을 돌려주고, 조각이 없으면 NO_ANSWER.
    그래서 citations 에 검색된 조각이 모두 나와, 어느 문서의 어떤 조각이 찾아졌는지 볼 수 있다.
    배포된 서버에서도 기본값이라, 실제 답으로 착각하지 않게 앞에 표시를 붙인다. 조각 글은 되풀이하지 않는다."""
    name = "fake"
    model = "fake"
    MARK = "[시험용 답변]"
    _CHUNK = re.compile(r"^\[(\d+)\] \(", re.M)        # answer.build_prompt 의 조각 머리 "[n] (제목, 위치)"

    def generate(self, system, user, timeout_s):
        refs = self._CHUNK.findall(user)
        if not refs:
            return Reply("NO_ANSWER")
        return Reply(f"{self.MARK} 실제 AI 를 연결하기 전의 시험용 답입니다. "
                     f"검색된 근거 {len(refs)}개를 모두 달았습니다 {''.join(f'[{n}]' for n in refs)}.")


class GeminiLLM:
    """google-genai. 재시도는 끄고(attempts=1), 시간 제한은 호출마다 밀리초로 넘긴다."""
    name = "gemini"

    def __init__(self, model, api_key, client=None):
        self.model = model
        if client is None:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key,
                                  http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)))
        self._client = client

    def generate(self, system, user, timeout_s):
        from google.genai import types
        try:
            r = self._client.models.generate_content(
                model=self.model, contents=user,
                config=types.GenerateContentConfig(system_instruction=system,
                                                   http_options=types.HttpOptions(timeout=int(timeout_s * 1000))))
        except Exception as e:
            raise _wrap(e) from None
        if not r.text:                                   # 안전 필터 등으로 글이 없으면 실패로 본다
            raise LLMError("빈 응답")
        u = r.usage_metadata
        out = None if u is None else (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        return Reply(r.text, None if u is None else u.prompt_token_count, out)


class OpenAILLM:
    """openai. 재시도는 끄고(max_retries=0), 시간 제한은 호출마다 초로 넘긴다. 요약 쪽과 같은 chat.completions 를 쓴다."""
    name = "openai"

    def __init__(self, model, api_key, client=None):
        self.model = model
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=api_key, max_retries=0)
        self._client = client

    def generate(self, system, user, timeout_s):
        try:
            r = self._client.with_options(timeout=timeout_s).chat.completions.create(
                model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        except Exception as e:
            raise _wrap(e) from None
        text = r.choices[0].message.content if r.choices else None
        if not text:
            raise LLMError("빈 응답")
        u = r.usage
        return Reply(text, None if u is None else u.prompt_tokens, None if u is None else u.completion_tokens)


def make_llm(env) -> tuple[LLM, float]:
    """환경변수로 LLM 과 시간 제한을 만든다. 설정이 잘못되면 LLMConfigError (키 값은 메시지에 넣지 않는다)."""
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
    if provider == "fake":
        return FakeLLM(), timeout_s

    model = (env.get("RAG_LLM_MODEL") or "").strip()
    if not model:
        raise LLMConfigError(f"RAG_LLM_PROVIDER={provider} 에는 RAG_LLM_MODEL 이 필요함")
    key = next((env[k] for k in _KEYS[provider] if env.get(k)), None)
    if not key:
        raise LLMConfigError(f"API 키가 없음 ({' 또는 '.join(_KEYS[provider])})")
    cls, package = (GeminiLLM, "google-genai") if provider == "gemini" else (OpenAILLM, "openai")
    try:
        return cls(model, key), timeout_s
    except ImportError:
        raise LLMConfigError(f"{provider} SDK({package}) 가 설치되지 않음") from None
