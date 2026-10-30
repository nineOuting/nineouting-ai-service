"""LLM 공급자 시험. 실제 API 는 부르지 않는다 (SDK 클라이언트를 흉내 낸 가짜를 끼운다). 비용·키 필요 없음."""
import sys
from types import SimpleNamespace as NS

import pytest

from rag.llm import GeminiLLM, LLMConfigError, LLMError, LLMTimeout, OpenAILLM, make_llm

GEMINI_ENV = {"RAG_LLM_PROVIDER": "gemini", "RAG_LLM_MODEL": "gemini-test", "GEMINI_API_KEY": "k" * 20}


# ---- 설정 ----

@pytest.mark.parametrize("env, word", [
    ({"RAG_LLM_PROVIDER": "anthropic"}, "지원하지 않는"),
    ({"RAG_LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"}, "RAG_LLM_MODEL"),
    ({"RAG_LLM_PROVIDER": "gemini", "RAG_LLM_MODEL": "m"}, "GEMINI_API_KEY"),
    ({"RAG_LLM_PROVIDER": "openai", "RAG_LLM_MODEL": "m"}, "OPENAI_API_KEY"),
    ({"RAG_LLM_TIMEOUT_S": "abc"}, "숫자"),
])
def test_설정_오류(env, word):
    with pytest.raises(LLMConfigError) as e:
        make_llm(env)
    assert word in str(e.value)


def test_설정_오류_메시지에_키_값이_없음(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)        # SDK 가 없는 상태
    secret = "sk-비밀키-값"
    with pytest.raises(LLMConfigError) as e:
        make_llm({"RAG_LLM_PROVIDER": "openai", "RAG_LLM_MODEL": "m", "OPENAI_API_KEY": secret})
    assert "SDK(openai)" in str(e.value) and secret not in str(e.value)


def test_GOOGLE_API_KEY_도_받음():
    pytest.importorskip("google.genai")
    llm, timeout_s = make_llm({"RAG_LLM_PROVIDER": "gemini", "RAG_LLM_MODEL": "m", "GOOGLE_API_KEY": "k" * 20,
                               "RAG_LLM_TIMEOUT_S": "12"})
    assert llm.name == "gemini" and llm.model == "m" and timeout_s == 12.0


def test_gemini_SDK_가_없으면_설정_오류(monkeypatch):
    monkeypatch.setitem(sys.modules, "google.genai", None)
    with pytest.raises(LLMConfigError) as e:
        make_llm(GEMINI_ENV)
    assert "google-genai" in str(e.value)


def test_gemini_클라이언트는_재시도를_끔():
    pytest.importorskip("google.genai")
    llm, _ = make_llm(GEMINI_ENV)
    retry = llm._client._api_client._http_options.retry_options
    assert retry is not None and retry.attempts == 1


# ---- Gemini (가짜 클라이언트) ----

class FakeGeminiModels:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def generate_content(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.result


def gemini(result=None, error=None):
    pytest.importorskip("google.genai")
    models = FakeGeminiModels(result, error)
    return GeminiLLM("gemini-test", "k", client=NS(models=models)), models


def test_gemini_답과_토큰():
    usage = NS(prompt_token_count=900, candidates_token_count=80, thoughts_token_count=40)
    llm, models = gemini(NS(text="답 [1].", usage_metadata=usage))
    r = llm.generate("시스템", "사용자", 7.5)
    assert (r.text, r.input_tokens, r.output_tokens) == ("답 [1].", 900, 120)     # 추론 토큰은 출력에 더함
    kw = models.calls[0]
    assert kw["model"] == "gemini-test" and kw["contents"] == "사용자"
    assert kw["config"].system_instruction == "시스템" and kw["config"].http_options.timeout == 7500


def test_gemini_사용량이_없어도_답은_돌려줌():
    llm, _ = gemini(NS(text="답 [1].", usage_metadata=None))
    r = llm.generate("s", "u", 1)
    assert r.text == "답 [1]." and r.input_tokens is None


def test_gemini_빈_응답은_실패():
    llm, _ = gemini(NS(text=None, usage_metadata=None))
    with pytest.raises(LLMError) as e:
        llm.generate("s", "u", 1)
    assert not isinstance(e.value, LLMTimeout)


def test_gemini_시간_초과는_LLMTimeout():
    import httpx
    llm, _ = gemini(error=httpx.ReadTimeout("느림"))
    with pytest.raises(LLMTimeout):
        llm.generate("s", "u", 1)


def test_gemini_API_오류는_LLMError_이고_메시지에_내용이_없음():
    from google.genai import errors
    llm, _ = gemini(error=errors.ClientError(400, {"error": {"message": "요청에 담긴 회의 내용"}}))
    with pytest.raises(LLMError) as e:
        llm.generate("s", "u", 1)
    assert not isinstance(e.value, LLMTimeout)
    assert str(e.value) == "ClientError" and e.value.__cause__ is None


# ---- OpenAI (가짜 클라이언트, SDK 없이) ----

class APITimeoutError(Exception):        # openai SDK 의 예외 이름과 같게
    pass


class FakeOpenAI:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls, self.timeouts = result, error, [], []
        self.chat = NS(completions=NS(create=self._create))

    def with_options(self, timeout):
        self.timeouts.append(timeout)
        return self

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.result


def openai_reply(text, usage=NS(prompt_tokens=700, completion_tokens=60)):
    return NS(choices=[NS(message=NS(content=text))], usage=usage)


def test_openai_답과_토큰과_시간_제한():
    client = FakeOpenAI(openai_reply("답 [2]."))
    r = OpenAILLM("gpt-test", "k", client=client).generate("시스템", "사용자", 9.0)
    assert (r.text, r.input_tokens, r.output_tokens) == ("답 [2].", 700, 60)
    assert client.timeouts == [9.0]
    assert client.calls[0]["model"] == "gpt-test"
    assert client.calls[0]["messages"] == [{"role": "system", "content": "시스템"}, {"role": "user", "content": "사용자"}]


@pytest.mark.parametrize("error, timeout", [(APITimeoutError("느림"), True), (RuntimeError("RateLimit 흉내"), False)])
def test_openai_예외_변환(error, timeout):
    with pytest.raises(LLMError) as e:
        OpenAILLM("gpt-test", "k", client=FakeOpenAI(error=error)).generate("s", "u", 1)
    assert isinstance(e.value, LLMTimeout) is timeout


def test_openai_빈_응답은_실패():
    with pytest.raises(LLMError):
        OpenAILLM("gpt-test", "k", client=FakeOpenAI(openai_reply(""))).generate("s", "u", 1)
