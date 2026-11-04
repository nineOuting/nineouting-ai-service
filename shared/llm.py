"""LLM 연결. RAG 답변(rag/)과 회의록 초안(minutes/)이 같이 쓴다. 공급자를 바꿔 끼울 수 있게 generate() 하나만 둔다.

설정 이름은 부르는 쪽이 앞부분(prefix)을 넘긴다. RAG 는 RAG_LLM, 회의록 초안은 MINUTES_LLM (minutes/llm_config.py).
  {prefix}_PROVIDER   fake (기본, 시험용 가짜) / gemini (시험) / openai (운영)
  {prefix}_MODEL      공급자의 모델 이름. gemini·openai 는 꼭 넣는다 (기본값 없음)
  {prefix}_TIMEOUT_S  응답을 기다리는 상한 (기본값은 부르는 쪽이 정한다). 재시도는 하지 않는다 (기다림이 길어지지 않게)
  API 키              GEMINI_API_KEY(또는 GOOGLE_API_KEY) / OPENAI_API_KEY. 요약 서버와 같은 이름

폴백 모델 (서버만. add_fallback 을 부른 곳에서만 붙는다. 평가·손 시험 도구는 붙이지 않는다)
  {prefix}_FALLBACK_PROVIDER    폴백 공급자. 모델과 함께 넣는다 (둘 다 비면 폴백 없음)
  {prefix}_FALLBACK_MODEL       폴백 모델 이름
  {prefix}_FALLBACK_COOLDOWN_S  기본 모델이 실패한 뒤 기본 모델을 건너뛰고 폴백만 부르는 시간 (기본 60초)

모델·키·SDK 가 없으면 LLMConfigError 를 낸다. 서버는 그대로 뜨고 그 기능의 API 만 503 이다.
공급자 SDK 의 예외는 LLMTimeout(시간 초과)·LLMError(그 밖) 로 바꿔서 던진다. 메시지에는 예외 이름만 넣는다.
SDK: gemini 는 공용 requirements.txt 의 google-genai, openai(운영)는 shared/requirements.txt 의 openai.
"""
import logging
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Protocol

PROVIDERS = ("fake", "gemini", "openai")
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_COOLDOWN_S = 60.0
_KEYS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "openai": ("OPENAI_API_KEY",)}
_TIMEOUT_ERRORS = {"TimeoutException", "ReadTimeout", "ConnectTimeout", "APITimeoutError", "TimeoutError"}

log = logging.getLogger("shared.llm")


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
    model: str | None = None                  # 실제로 답한 "공급자/모델". 폴백을 붙였을 때만 채운다
    fallback: bool = False                    # 폴백 모델이 답했으면 True


class LLM(Protocol):
    name: str
    model: str

    def generate(self, system: str, user: str, timeout_s: float, effort: str | None = None) -> Reply: ...
    # effort: 생각하는 양 ("low" 등). None 이면 모델 기본값. OpenAI 만 쓰고(reasoning_effort) 나머지는 무시한다.
    # 부르는 쪽은 effort 가 있을 때만 넘긴다 (시험용 가짜 AI 가 이 인자를 몰라도 되게)


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

    def generate(self, system, user, timeout_s, effort=None):
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

    def generate(self, system, user, timeout_s, effort=None):
        """effort 는 쓰지 않는다 (Gemini 는 생각 설정 방식이 달라 모델 기본값 그대로)."""
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

    def generate(self, system, user, timeout_s, effort=None):
        """effort 가 있으면 reasoning_effort 로 넘긴다. 이 값을 받지 않는 모델이면 호출 오류(LLMError)가 된다."""
        extra = {"reasoning_effort": effort} if effort else {}
        try:
            r = self._client.with_options(timeout=timeout_s).chat.completions.create(
                model=self.model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **extra)
        except Exception as e:
            raise _wrap(e) from None
        text = r.choices[0].message.content if r.choices else None
        if not text:
            raise LLMError("빈 응답")
        u = r.usage
        return Reply(text, None if u is None else u.prompt_tokens, None if u is None else u.completion_tokens)


def make_llm(env, prefix: str, timeout_key: str | None = None,
             default_timeout_s: float = DEFAULT_TIMEOUT_S) -> tuple[LLM, float]:
    """환경변수로 LLM 과 시간 제한을 만든다. {prefix}_PROVIDER·{prefix}_MODEL 을 읽고,
    시간 제한은 timeout_key(없으면 {prefix}_TIMEOUT_S)를 읽는다.
    설정이 잘못되면 LLMConfigError (키 값은 메시지에 넣지 않는다)."""
    timeout_key = timeout_key or f"{prefix}_TIMEOUT_S"
    provider = env.get(f"{prefix}_PROVIDER", "fake")
    if provider not in PROVIDERS:
        raise LLMConfigError(f"지원하지 않는 {prefix}_PROVIDER: {provider} (가능: {', '.join(PROVIDERS)})")
    raw = env.get(timeout_key, default_timeout_s)
    try:
        timeout_s = float(raw)
    except (TypeError, ValueError):
        raise LLMConfigError(f"{timeout_key} 는 숫자여야 함") from None
    if timeout_s <= 0:
        raise LLMConfigError(f"{timeout_key} 는 0보다 커야 함")
    model = (env.get(f"{prefix}_MODEL") or "").strip()
    if provider != "fake" and not model:
        raise LLMConfigError(f"{prefix}_PROVIDER={provider} 에는 {prefix}_MODEL 이 필요함")
    return _build(provider, model, env), timeout_s


def _build(provider, model, env) -> LLM:
    """공급자·모델로 LLM 을 만든다 (이름 검사는 부르는 쪽이 한다). 키·SDK 가 없으면 LLMConfigError."""
    if provider == "fake":
        return FakeLLM()
    key = next((env[k] for k in _KEYS[provider] if env.get(k)), None)
    if not key:
        raise LLMConfigError(f"API 키가 없음 ({' 또는 '.join(_KEYS[provider])})")
    cls, package = (GeminiLLM, "google-genai") if provider == "gemini" else (OpenAILLM, "openai")
    try:
        return cls(model, key)
    except ImportError:
        raise LLMConfigError(f"{provider} SDK({package}) 가 설치되지 않음") from None


def label(llm) -> str:
    """로그에 쓰는 "공급자/모델"."""
    return f"{llm.name}/{llm.model}"


class FallbackLLM:
    """기본 모델 호출이 실패(LLMError: 연결 실패·시간 초과·빈 응답·429·5xx·404·401 등 호출 오류 전부)하면 같은 입력으로 폴백 모델을 한 번 부른다.
    기본 모델이 실패하면 cooldown_s 동안은 기본 모델을 건너뛰고 폴백만 부른다 (장애 중에 요청마다 시간 제한을 기다리지 않게).
    그 시간이 지나면 기본 모델을 다시 부른다. 폴백도 실패하면 폴백의 예외를 그대로 던진다.
    답을 받은 뒤의 검사 실패(근거 번호, 회의록 형식)는 호출이 성공한 것이라 폴백하지 않는다.
    답변 API 는 여러 스레드에서 부르므로 건너뛰는 시각은 잠금 안에서 읽고 쓴다. 로그에는 모델 이름과 예외 이름만 남긴다."""

    def __init__(self, primary: LLM, fallback: LLM, cooldown_s: float = DEFAULT_COOLDOWN_S, clock=time.monotonic):
        self.primary, self.fallback, self.cooldown_s, self._clock = primary, fallback, cooldown_s, clock
        self.name, self.model = primary.name, primary.model      # 설정상 기본 모델. 실제로 답한 모델은 Reply.model
        self._down_until = None                                  # 이 시각까지 기본 모델을 건너뛴다. None 이면 정상
        self._lock = threading.Lock()

    def generate(self, system, user, timeout_s, effort=None):
        extra = {"effort": effort} if effort else {}           # 받은 그대로 두 모델에 넘긴다
        with self._lock:
            skip = self._down_until is not None and self._clock() < self._down_until
        if not skip:
            try:
                reply = self.primary.generate(system, user, timeout_s, **extra)
            except LLMError as e:
                with self._lock:
                    self._down_until = self._clock() + self.cooldown_s
                log.warning("기본 모델 실패, 폴백 모델로: primary=%s fallback=%s error=%s cooldown_s=%g",
                            label(self.primary), label(self.fallback), e, self.cooldown_s)
            else:
                with self._lock:
                    recovered, self._down_until = self._down_until is not None, None
                if recovered:
                    log.info("기본 모델 다시 정상: primary=%s", label(self.primary))
                return replace(reply, model=label(self.primary))
        reply = self.fallback.generate(system, user, timeout_s, **extra)
        return replace(reply, model=label(self.fallback), fallback=True)


def add_fallback(llm: LLM, env, prefix: str) -> LLM:
    """{prefix}_FALLBACK_PROVIDER·{prefix}_FALLBACK_MODEL 이 있으면 llm 을 FallbackLLM 으로 감싼다.
    둘 다 없으면 llm 을 그대로 돌려준다. 공급자와 모델은 함께 넣는다 (한쪽만 있으면 LLMConfigError).
    폴백이 기본 모델과 같으면 감싸지 않는다 (같은 모델을 두 번 부르지 않게).
    서버에서만 부른다. 평가·손 시험 도구는 부르지 않으므로 설정이 있어도 폴백하지 않는다."""
    p_key, m_key, c_key = f"{prefix}_FALLBACK_PROVIDER", f"{prefix}_FALLBACK_MODEL", f"{prefix}_FALLBACK_COOLDOWN_S"
    provider = (env.get(p_key) or "").strip()
    model = (env.get(m_key) or "").strip()
    if not provider and not model:
        return llm
    if not provider:
        raise LLMConfigError(f"{m_key} 는 {p_key} 와 함께 넣어야 함")
    if provider not in PROVIDERS:
        raise LLMConfigError(f"지원하지 않는 {p_key}: {provider} (가능: {', '.join(PROVIDERS)})")
    if provider != "fake" and not model:
        raise LLMConfigError(f"{p_key}={provider} 에는 {m_key} 이 필요함")
    raw = env.get(c_key, DEFAULT_COOLDOWN_S)
    try:
        cooldown_s = float(raw)
    except (TypeError, ValueError):
        raise LLMConfigError(f"{c_key} 는 숫자여야 함") from None
    if cooldown_s < 0:
        raise LLMConfigError(f"{c_key} 는 0 이상이어야 함")
    fallback = _build(provider, model, env)
    if label(fallback) == label(llm):
        log.info("폴백 모델이 기본 모델과 같아 폴백 없이 씀: %s (%s_*)", label(llm), f"{prefix}_FALLBACK")
        return llm
    return FallbackLLM(llm, fallback, cooldown_s)


def describe(llm: LLM) -> str:
    """시작 로그용. 폴백이 있으면 "openai/a, 폴백 openai/b (건너뛰기 60초)"."""
    if isinstance(llm, FallbackLLM):
        return f"{label(llm.primary)}, 폴백 {label(llm.fallback)} (건너뛰기 {llm.cooldown_s:g}초)"
    return label(llm)
