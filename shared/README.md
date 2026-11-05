# shared

RAG 답변(`rag/`)과 회의록 초안(`minutes/`)이 같이 쓰는 코드다. 요약 기능(`summary/`, `server/`)은 대상이 아니다.

| 파일 | 하는 일 |
|---|---|
| `llm.py` | AI 연결. 공급자(`fake`·`gemini`·`openai`)를 바꿔 끼울 수 있게 `generate()` 하나만 둔다. 설정 이름의 앞부분은 부르는 쪽이 넘긴다 (RAG `RAG_LLM`, 회의록 초안 `MINUTES_LLM`) |
| `llm.py` 의 `add_fallback`, `FallbackLLM` | 폴백 모델. `{앞부분}_FALLBACK_PROVIDER`·`_MODEL`(함께 넣는다)·`_COOLDOWN_S`(기본 60초). 기본 모델 호출이 실패하면(`LLMError`) 같은 입력으로 폴백을 한 번 부르고, 정한 시간 동안 기본 모델을 건너뛴다. 서버에서 AI 를 만드는 곳에서만 부른다 (평가·손 시험 도구는 폴백하지 않는다) |
| `masking.py`, `llm.py` 의 `MaskingLLM` | 외부 AI 로 보내는 글의 개인정보 가리기. 주민등록번호·카드번호·이메일·IPv4 주소·전화번호·계좌번호·번지까지 있는 주소·차량번호, 8자리 이상 숫자와 `1234-5678` 모양, 한글로 7자 이상 읽은 번호, 비밀번호·인증번호·생년월일이라는 말 바로 뒤의 숫자를 `<PHONE_1>` 같은 대체 표시로 바꿔 보내고, 답을 받으면 되돌린다. 실제 공급자(`gemini`·`openai`)는 만들 때 자동으로 감싼다. 형식이 뚜렷한 값이 나가는 것을 줄이는 기능이고 보장은 아니다. 사람 이름, 단서 없는 짧은 숫자(뒷 4자리, 우편번호, 동·호수), 자릿수로 읽은 번호, 두 줄로 나뉜 번호, 지역만 말한 주소, IPv6 는 가리지 못한다 |
| `envfile.py` | 로컬 설정 파일 읽기. 손 시험·평가 도구만 쓴다 (서버는 환경변수만 본다). 순서는 환경변수 → 저장소 루트 `.env` → `rag/.env`(옛 위치, 옮기는 동안만 읽고 경고) |
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
