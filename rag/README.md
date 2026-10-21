# rag

RAG 파이프라인 (문서 넣기·검색·답변). 작업 규칙은 `AGENTS.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 회의록 로더, 512토큰 나누기, 임베딩 | 구현 |
| 저장(pgvector, 버전 교체), 검색(workspaceId + docIds) | 구현 (함수까지, HTTP 없음) |
| 대기열·작업자, 완료 콜백 | 구현 |
| 내부 API (문서 넣기·상태·삭제) | 구현. 회의록(`sourceType=minutes`)만 받음 |
| 업로드 문서(PDF·워드·엑셀), 답변 | 예정 |

원본 명세는 노션 "RAG 인터페이스 명세".

## 준비 (저장소 루트에서)

Python 3.12 (`Dockerfile`과 같음)

```bash
uv venv --python 3.12 --seed .venv        # uv 가 없으면: python3.12 -m venv .venv
.venv/bin/pip install -r rag/requirements-dev.txt
```

임베딩 모델 (dragonkue/multilingual-e5-small-ko, 약 465MB, revision 고정)

```bash
.venv/bin/python -m rag.fetch_model
```

- 이미 있으면 접속하지 않는다. 받은 뒤 sha256 을 확인한다
- 서버와 시험은 모델을 받지 않는다. 캐시에 없으면 오류(시험은 건너뜀)

## DB (로컬 개발·시험용)

운영 DB 는 CN팀이 관리한다. 아래는 로컬에서만 쓴다. Postgres + pgvector 0.8 이상이면 되며, 방법은 둘 중 하나를 고른다.

먼저 접속 정보를 만든다 (공통).

```bash
cp rag/.env.example rag/.env   # 값을 채운다. 채팅·로그에 붙여 넣지 않는다. 로컬 전용이라 간단한 값이면 된다
```

### A. Homebrew (macOS, 상주 서비스 없이)

`rag/.env` 의 포트는 `5432` (기본값 그대로).

```bash
brew install postgresql@17 pgvector     # pgvector 0.8 이상인지 brew info pgvector 로 확인

PG=/opt/homebrew/opt/postgresql@17/bin
DATA=/opt/homebrew/var/postgresql@17
$PG/pg_ctl -D $DATA -l /opt/homebrew/var/log/postgresql@17.log -w start   # 띄우기
$PG/pg_ctl -D $DATA stop                                                  # 끄기

# 처음 한 번: 앱 계정·DB·vector 확장 만들기 (비밀번호는 rag/.env 에서 읽는다)
set -a; . rag/.env; set +a
$PG/psql -X -d postgres -v ON_ERROR_STOP=1 -f rag/db/init/01_roles.sql
```

- `brew services start` 를 쓰지 않으면 로그인할 때 자동으로 뜨지 않는다. 필요할 때만 띄운다
- Homebrew 기본 설정은 이 컴퓨터 안의 접속을 비밀번호 없이 허용한다(`trust`). 로컬에서는 비밀번호가 검사되지 않는다

### B. Docker

`rag/.env` 의 두 주소 포트를 `5433` 으로 바꾸고 `POSTGRES_PASSWORD` 도 채운다.

```bash
cd rag && docker compose up -d && cd ..   # pgvector/pgvector:0.8.0-pg17, 포트 127.0.0.1:5433
cd rag && docker compose down && cd ..    # 끄기 (데이터는 볼륨에 남음)
```

- 처음 띄울 때 `db/init/01_roles.sql` 이 자동으로 실행된다 (볼륨이 비어 있을 때만. 비밀번호를 바꾸려면 볼륨을 지우고 다시 띄운다)

### 마이그레이션 (공통)

```bash
set -a; . rag/.env; set +a                # 환경변수로 불러오기 (현재 셸에서만)
.venv/bin/python -m rag.migrate           # 개발 DB(rag)
.venv/bin/python -m rag.migrate --test    # 시험 DB(rag_test)
```

- 앱 계정 `rag_app` 은 관리자 권한이 없다 (운영의 전용 계정과 같은 조건)

## 내부 API (백엔드 → AI 서버)

`server/main.py` 에 라우터로 붙는다. `RAG_DATABASE_URL` 이 있을 때만 붙고, 없으면 요약만 뜬다.

**아래는 모두 같은 터미널에서 한다.** `rag/.env` 를 불러오지 않은 터미널에서 서버를 띄우면 RAG 가 꺼진 채로 떠서 RAG 주소가 `404 Not Found` 가 된다.

```bash
cd <저장소 루트> && set -a && . rag/.env && set +a
.venv/bin/pip install -r requirements.txt       # 요약 서버 의존성 (처음 한 번)
# DB 띄우기 (Homebrew 기준, 위 "DB" 참고). pg_ctl 은 전체 경로로 쓴다
/opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/log/postgresql@17.log start
.venv/bin/python -m rag.migrate                 # 새 마이그레이션이 있을 때마다
.venv/bin/uvicorn server.main:app --port 8000   # 로그에 "RAG 작업자 시작" 이 나오면 준비 끝
```

- 브라우저로 http://127.0.0.1:8000/docs 를 열면 주소별 설명·필드·예시가 있고, "Try it out" 으로 직접 호출할 수 있다
- `RAG_WORKER_IN_PROCESS=0` 이면 작업자는 띄우지 않는다 (`python -m rag.worker` 로 따로)

| 주소 | 응답 |
|---|---|
| `POST /internal/rag/documents` | 202 `{docId, docVersion, status: "accepted"}` |
| `GET /internal/rag/documents/{docId}` | 200 `{docId, docVersion, status, errorCode, chunkCount}`, 없으면 404 |
| `DELETE /internal/rag/documents/{docId}` | 204 (없어도) |
| `DELETE /internal/rag/workspaces/{workspaceId}` | 204 |

- 상태 값은 `accepted` / `processing` / `ready` / `failed` (DB 의 `queued` 를 `accepted` 로 보여 줌)
- 명세에 없어서 AI 쪽이 정한 것 (확인 필요): 입력 오류 400 `{"error": "INVALID_INPUT"}`, 같은 `docId` 가 다른 워크스페이스로 오면 409 `CONFLICT`, DB 를 쓸 수 없으면 503 `NOT_READY`, 넣기 응답 코드 202
- 지금은 `sourceType=minutes` 만 받는다. `upload` 는 400 (준비 중)
- 서버 간 인증은 `api.verify_internal_request` 자리만 있다 (CN팀 방식 대기)

## 작업자

접수(`jobs.submit()`)는 문서를 `queued` 로 두고 작업 표(`rag_jobs`)에 적기만 한다. 작업자가 꺼내서 로더 → 나누기 → 임베딩 → 저장을 하고 완료 콜백을 보낸다.

```bash
set -a; . rag/.env; set +a
.venv/bin/python -m rag.worker            # 따로 띄우기. Ctrl+C 나 SIGTERM 이면 처리 중인 작업을 마치고 멈춘다
```

API 프로세스 안에서 띄울 때는 `worker.start_in_thread(worker.from_env())` 를 쓴다.

| 규칙 | 내용 |
|---|---|
| 꺼내기 | `FOR UPDATE SKIP LOCKED`. 작업자를 여럿 띄워도 같은 작업을 두 번 잡지 않는다. `running` 으로 10분 넘게 멈춘 작업은 다시 잡는다 |
| 영구 실패 | `no_text`, 형식 오류 등은 재시도 없이 `failed` 콜백. 업로드 문서는 로더가 생길 때까지 `unsupported_format` (`last_error` 에 "업로드 로더 미구현") |
| 일시 실패 | 그 밖의 예외는 30초·2분 뒤 다시, 3번째도 실패하면 `internal_error` |
| 결과 버림 | 처리 중 문서가 지워졌거나 더 새 버전이 접수되면 콜백 없이 끝낸다 |
| 콜백 | `POST {callbackUrl}`, `X-Request-Id` 를 돌려준다. 408·429·5xx·연결 오류만 3번 재시도(1·2·4초), 리다이렉트는 따라가지 않는다 |
| 정리 | 끝난 작업은 `payload`(문서 내용)를 비우고, 7일 뒤 줄을 지운다 |
| DB 접속 | 시작할 때 접속하지 않고 반복을 돌며 접속한다. DB 가 늦게 뜨거나 `vector` 확장이 없어도 서버는 뜨고(그동안 RAG 주소는 503), 연결이 끊기면 다음 반복에서 다시 접속한다 |

## 시험

```bash
set -a; . rag/.env; set +a                # DB 시험을 돌리려면 (없으면 DB 시험은 건너뜀)
.venv/bin/python -m pytest rag/tests
```

| 환경변수 | 설명 |
|---|---|
| `RAG_TEST_DATABASE_URL` | 시험 DB 주소. 시험마다 표를 비우므로 개발 DB 주소를 넣지 않는다 |
| `RAG_REQUIRE_DB=1` | 시험 DB 에 접속할 수 없으면 건너뛰지 않고 실패시킨다 (CI 용) |
| `RAG_REQUIRE_MODEL=1` | 모델이 없으면 임베딩 시험을 건너뛰지 않고 실패시킨다 (CI 용) |
| `RAG_WORKER_IN_PROCESS` | `0` 이면 API 서버 안에서 작업자를 띄우지 않음 (`python -m rag.worker` 로 따로). 기본 `1` |
| `RAG_API_PREFIX` | 주소 앞부분 (기본 `/internal/rag`). CN팀 내부 주소 규칙에 맞출 때 |
| `RAG_REQUEST_ID_HEADER` | 요청 추적 헤더 이름 (기본 `X-Request-Id`) |
| `RAG_DB_POOL_MAX`, `RAG_DB_POOL_TIMEOUT_S` | API 의 DB 연결 풀 최대 개수(기본 5), 연결을 기다리는 시간(기본 5초, 넘으면 503) |
| `RAG_CALLBACK_ALLOWED_HOSTS` | 콜백을 보내도 되는 호스트 (`host` 또는 `host:port`, 쉼표로). 비어 있으면 모두 허용(개발용, 경고 로그). 운영에서는 반드시 설정 |
| `RAG_TORCH_THREADS` | 작업자의 임베딩 스레드 수 (기본 2). 같은 노드의 STT 와 CPU 를 나눠 쓰려고 |
| `RAG_DB_STATEMENT_TIMEOUT_MS`, `RAG_DB_IDLE_TX_TIMEOUT_MS` | 작업자 DB 연결의 시간 제한 (기본 30000, 60000) |
| `RAG_DB_CONNECT_TIMEOUT_S` | 작업자가 DB 에 접속할 때 기다리는 상한 (기본 10초). 닿지 않는 주소에서 서버 종료가 늦어지지 않게 |
| `HF_HUB_CACHE` | 모델 캐시 위치. 없으면 `~/.cache/huggingface/hub`. 이미지에서는 `/models` 로 고정할 예정 (실행 사용자가 바뀌어도 같은 곳을 찾도록) |
