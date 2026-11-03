# shared

RAG 답변(`rag/`)과 회의록 초안(`minutes/`)이 같이 쓰는 코드다. 요약 기능(`summary/`, `server/`)은 대상이 아니다.

| 파일 | 하는 일 |
|---|---|
| `llm.py` | AI 연결. 공급자(`fake`·`gemini`·`openai`)를 바꿔 끼울 수 있게 `generate()` 하나만 둔다. 설정 이름의 앞부분은 부르는 쪽이 넘긴다 (RAG `RAG_LLM`, 회의록 초안 `MINUTES_LLM`) |
| `llm.py` 의 `add_fallback`, `FallbackLLM` | 폴백 모델. `{앞부분}_FALLBACK_PROVIDER`·`_MODEL`(함께 넣는다)·`_COOLDOWN_S`(기본 60초). 기본 모델 호출이 실패하면(`LLMError`) 같은 입력으로 폴백을 한 번 부르고, 정한 시간 동안 기본 모델을 건너뛴다. 서버에서 AI 를 만드는 곳에서만 부른다 (평가·손 시험 도구는 폴백하지 않는다) |
| `callback.py` | 처리가 끝났음을 백엔드에 알리는 완료 콜백. 허용 호스트 설정 이름(`key`)과 로그에 남길 대상 표시(`ref`)는 부르는 쪽이 넘긴다 |

설정 값과 기본값은 각 기능의 README 에 있다 (`rag/README.md`, `minutes/README.md`).

## 시험

```bash
.venv/bin/python -m pytest shared/tests
```

여기를 고치면 두 기능의 시험도 같이 돌린다.

```bash
.venv/bin/python -m pytest shared/tests rag/tests minutes/tests
```

- 실제 AI 는 부르지 않는다. 공급자 SDK 클라이언트를 흉내 낸 가짜를 끼운다
