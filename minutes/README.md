# minutes

회의록 초안 (AI-10). 회의 끝 요약(안건 보드 `todaySummary`)과 사용자가 고른 양식을 받아, 양식의 칸마다 근거가 붙은 줄 목록을 만든다. 명세는 `docs/api/minutes-draft-contract.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 재료 만들기 (요약 → 줄 재료), 칸 배치(AI), 코드 검사, 줄 글 | 구현. AI 는 칸 고르기와 결정 문장 다듬기만 한다 |
| 접수 주소 `POST /internal/minutes/drafts`, 대기열·작업자, 결과 콜백 | 구현 |
| 양식 변경 시 다시 배치 (`previousLines`) | 예정 (SCRUM-98). 지금은 400 "준비 중" |
| 근거·맥락 연결 (AI-11) | ai-service API 없음. 결과의 `sources`·`transcriptSeqs` 를 백엔드가 따라간다 |

## 흐름

```
백엔드 ──POST /internal/minutes/drafts──▶ 접수 (api.py): 형식 검사 → minutes_jobs 에 넣기 → 202
                                          │
                                 작업자 (worker.py): 꺼내기 → 초안 만들기 (draft.py) → 콜백 → 끝내기
                                          │
백엔드 ◀──POST {callbackUrl}───────────────┘  SUCCEEDED (sections, unplaced, unplacedLines) / FAILED (error.code)
```

| 파일 | 하는 일 |
|---|---|
| `request.py` | 요청 형식 검사 (명세 2.1, 7.5 상한) |
| `materials.py` | 요약 항목 → 재료 (명세 7.1). 요약 문장·의견·할 일은 글을 고정하고, 결정만 AI 가 다듬는다 |
| `placement.py` | AI 에게 칸을 고르게 하고 결과를 코드로 검사, 걸리면 이유를 붙여 다시 시킴 (명세 7.2·7.3) |
| `lines.py` | 칸별 줄 목록과 `content` 만들기 |
| `draft.py` | 위를 묶어 결과 JSON 하나를 만든다 (`make_draft`) |
| `jobs.py`, `worker.py` | 대기열 표와 작업자 |
| `api.py`, `runtime.py` | 접수 주소와 `server/main.py` 에 붙이는 곳 |
| `prompts/placement/v*.md` | 칸 배치 지시문. `MINUTES_PROMPT_VERSION` 이 없으면 번호가 가장 큰 파일 |

## 서버에 붙이기

`server/main.py` 는 `MINUTES_DATABASE_URL` 이 있을 때만 초안 기능을 붙인다. 없으면 이 주소는 404 이고, 요약·RAG 는 그대로다. RAG 와 같은 PostgreSQL 을 써도 되지만 연결 풀·마이그레이션·설정은 따로다.

```bash
cd <저장소 루트> && set -a && . rag/.env && set +a     # DB 접속 정보와 RAG_LLM_* (rag/README.md 의 "DB")
export MINUTES_DATABASE_URL="$RAG_DATABASE_URL"        # 로컬에서는 RAG 개발 DB 를 같이 써도 된다
.venv/bin/python -m minutes.migrate                    # minutes_jobs 표 (새 마이그레이션이 있을 때마다)
.venv/bin/uvicorn server.main:app --port 8000          # 로그에 "회의록 초안 작업자 시작" 이 나오면 준비 끝
```

| 환경변수 | 설명 | 기본값 |
|---|---|---|
| `MINUTES_DATABASE_URL` | 대기열 DB. 있으면 초안 기능이 켜진다 | 없음 |
| `MINUTES_CALLBACK_ALLOWED_HOSTS` | 콜백을 보낼 수 있는 `호스트:포트` (쉼표 구분). 운영에서는 반드시 넣는다. 비우면 모두 허용 (개발용) | 없음 |
| `RAG_LLM_PROVIDER`, `RAG_LLM_MODEL`, `GEMINI_API_KEY` | AI 설정. RAG 답변과 같은 값을 쓴다. 틀리면 서버는 뜨고 초안 주소만 503 | `fake` |
| `MINUTES_LLM_TIMEOUT_S` | AI 응답 시간 제한(초) | 60 |
| `MINUTES_PROMPT_VERSION` | 지시문 버전 (예: `v6`) | 가장 큰 번호 |
| `MINUTES_WORKER_IN_PROCESS` | `0` 이면 서버 안에서 작업자를 띄우지 않는다 (`python -m minutes.worker` 로 따로) | `1` |
| `MINUTES_DB_POOL_MAX`, `MINUTES_DB_POOL_TIMEOUT_S` | 접수 주소의 DB 연결 수, 연결을 기다리는 초 | 3, 5 |
| `MINUTES_DB_STATEMENT_TIMEOUT_MS`, `MINUTES_DB_IDLE_TX_TIMEOUT_MS`, `MINUTES_DB_CONNECT_TIMEOUT_S` | DB 시간 제한 | 30000, 60000, 10 |

## 작업자

| 규칙 | 내용 |
|---|---|
| 꺼내기 | `FOR UPDATE SKIP LOCKED`. 작업자를 여럿 띄워도 같은 작업을 두 번 잡지 않는다. `running` 으로 10분 넘게 멈춘 작업은 다시 잡는다 |
| 한 번에 | 작업자 하나는 한 번에 요청 하나를 처리한다. 여러 개가 오면 표에 쌓였다가 순서대로 나간다 |
| AI 실패·검사 실패 | 초안 만들기 안에서 처리하고 `FAILED` 결과로 콜백한다 (`LLM_TIMEOUT`, `LLM_FAILED`, `CHECK_FAILED`) |
| 예상하지 못한 오류 | 30초·2분 뒤 다시, 3번째도 실패하면 `INTERNAL_ERROR` |
| 콜백 | 408·429·5xx·연결 오류만 3번 재시도(1·2·4초). 끝내 못 보내면 작업을 `failed` 로 끝내고, 같은 `requestId` 가 다시 오면 새로 받는다 |
| 정리 | 끝난 작업은 `payload`(요약 내용)를 비우고, 7일 뒤 줄을 지운다 |
| 로그 | 요약·칸 이름·초안 글은 남기지 않는다. 작업·요청 번호, 시도 횟수, 결과, 오류 코드, 걸린 시간만 |

## 손으로 시험하기

서버 없이 요청 JSON 하나로 초안을 만들어 칸별 글을 본다.

```bash
.venv/bin/python -m minutes.try_draft                           # 합성 요청, 가짜 AI (비용 없음)
.venv/bin/python -m minutes.try_draft --real                    # 실제 AI (rag/.env 의 RAG_LLM_* 와 키). 호출마다 요금이 나간다
.venv/bin/python -m minutes.try_draft --file 요청.json --real --show-io   # AI 입력·출력·토큰까지
```

- 실제 AI 를 쓰면 요약 내용이 공급자(Google)로 전송된다
- 실제 회의 데이터는 레포 밖에 두고 `--file` 로 경로만 넘긴다

## 시험

```bash
.venv/bin/python -m pytest minutes/tests
```

- DB 시험은 시작할 때 마이그레이션을 직접 돌린다. 시험 DB 는 `MINUTES_TEST_DATABASE_URL`(없으면 `RAG_TEST_DATABASE_URL`)을 쓰고, 시험마다 그리고 끝날 때 `minutes_jobs` 만 비운다. DB 가 없으면 건너뛰고, `MINUTES_REQUIRE_DB=1` 이면 실패시킨다
- AI 는 가짜를 쓴다. 비용이 없다
- 시험 샘플은 합성 데이터만 `minutes/tests/fixtures/` 에 둔다
