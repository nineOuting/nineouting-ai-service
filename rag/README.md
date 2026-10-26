# rag

RAG 파이프라인 (문서 넣기·검색·답변). 작업 규칙은 `AGENTS.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 회의록 로더, 512토큰 나누기, 임베딩 | 구현. 확정 회의록(`formatVersion` 1.0, 칸의 줄 하나가 조각 하나)과 임시 형식(`schema_version` 0.1)을 받음 |
| 저장(pgvector, 버전 교체), 검색(workspaceId + docIds) | 구현 |
| 대기열·작업자, 완료 콜백 | 구현 |
| 내부 API (문서 넣기·상태·삭제) | 구현. 회의록(`sourceType=minutes`)만 받음 |
| 배포 이미지 (`ghcr.io/nineouting/nineouting-ai-service`) | 구현. 아래 "이미지·배포" |
| 답변 API (검색 → LLM → 근거 번호 검사) | 구현. LLM 은 설정으로 고름: 가짜(기본, 답 앞에 `[시험용 답변]`) / Gemini(시험) / OpenAI(운영, SDK 는 모델을 정할 때 추가) |
| 업로드 문서(PDF·워드·엑셀) | 받기·형식 확인(SCRUM-106), PDF 읽기(SCRUM-107, PDFium) 구현. 워드·엑셀 읽기는 예정 (SCRUM-108·109) |

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
| `POST /internal/rag/documents` | 202 `{docId, docVersion, status: "accepted"}`. 저장된 것보다 낮은 회의록 버전이면 `"ignored"` (처리·콜백 없음) |
| `GET /internal/rag/documents/{docId}` | 200 `{docId, docVersion, status, errorCode, chunkCount}`, 없으면 404 |
| `DELETE /internal/rag/documents/{docId}` | 204 (없어도) |
| `DELETE /internal/rag/workspaces/{workspaceId}` | 204 |

- 상태 값은 `accepted` / `processing` / `ready` / `failed` (DB 의 `queued` 를 `accepted` 로 보여 줌)
- 명세에 없어서 AI 쪽이 정한 것 (확인 필요): 입력 오류 400 `{"error": "INVALID_INPUT"}`, 같은 `docId` 가 다른 워크스페이스로 오면 409 `CONFLICT`, DB 를 쓸 수 없으면 503 `NOT_READY`, 넣기 응답 코드 202
- `sourceType=upload` 는 허용 저장소(`RAG_UPLOAD_ALLOWED_HOSTS`)가 설정됐을 때만 받는다. 없으면 400. `fileUrl` 의 호스트가 허용 저장소가 아니어도 400
  - 작업자가 파일을 메모리로 받아(디스크에 쓰지 않음) 안쪽 모양으로 형식을 알아낸다. 확장자는 믿지 않는다
  - 실패: 서명 만료 `url_expired`, 그 밖에 받지 못함 `download_failed` (둘 다 다시 시도하지 않음. 백엔드가 다시 호출), 크기·푼 크기 초과 `too_large`, 암호 `encrypted`, pdf·docx·xlsx 가 아님(사용자가 묶은 zip, 옛 doc·xls, hwp 포함) `unsupported_format`
  - PDF: 쪽 하나가 조각 재료 하나, 근거 위치 `{type: "page", page, label: "3쪽"}`. 글 없는 쪽은 건너뛰고, 모든 쪽에 글이 없으면(스캔 PDF 등) `no_text`, 1,000쪽 넘으면 `too_large`. PDFium(`pypdfium2`)으로 읽는다. pypdf 는 글자 폭 정보가 어긋난 PDF 에서 글자마다 띄어 꺼내서 쓰지 않는다
  - 워드·엑셀은 읽기가 생기기 전까지 형식을 알아낸 뒤 `unsupported_format` 으로 끝난다 (`last_error` 에 "업로드 로더 미구현 (docx)" 처럼 형식)
  - 머리글·바닥글·쪽 번호 빼기 같은 정제는 아직 없다 (SCRUM-110)
  - 시험 샘플(합성): `rag/tests/fixtures/upload_*.pdf`, 만드는 스크립트 `make_upload_samples.py` (fpdf2, Noto Sans CJK KR)
  - `fileUrl` 에는 접근 서명이 들어 있어 로그·오류 글에 남기지 않는다
- `minutes` 형식 (회의록 초안 명세 `docs/api/minutes-draft-contract.md` 4장)
  - 확정 회의록 `formatVersion: "1.0"`: `meetingId`, `meetingStartAt`, `confirmedAt`(선택), `sections[{title, content}]`. `content` 의 줄 하나가 조각 하나이고 줄 앞 `- `·`* `·`• ` 는 뗀다. 빈 줄과 "해당 내용 없음" 은 넣지 않는다. 칸 이름을 맥락으로 붙여 검색하고, 근거 위치는 `{type: "section", section, label}` (칸 이름)
  - 임시 형식 `schema_version: "0.1"` (summary.json): 전환 기간에만. 근거 위치는 녹음 시간 `{type: "time", startMs, endMs, label}`
  - 둘 다 아니거나 확정 회의록 모양이 틀리면 400 (어느 칸이 틀렸는지 알려 줌)
- 회의록 버전이 저장된 것과 새 것 모두 정수 모양(`"5"`)이면 숫자로 비교해 낮은 버전은 무시한다. 재시도로 옛 확정본이 늦게 와서 새 확정본을 덮어쓰지 않게 하려는 것. 정수 모양이 아니면 마지막에 받은 것을 쓴다
- 서버 간 인증은 `api.verify_internal_request` 자리만 있다 (CN팀 방식 대기)

### 답변 `POST /internal/rag/answer`

요청 `{workspaceId, docIds, query, topK?}` (ID 는 모두 정수, topK 1~20 기본 5, 그 밖의 필드는 무시) → 200 `{status, answer, citations, errorCode}`

| 단계 | 결과 |
|---|---|
| `docIds` 가 비었음 | `no_result` (DB·LLM 을 부르지 않음) |
| `workspaceId`·`docIds` 로 거른 검색에서 조각 0개 | `no_result` (LLM 을 부르지 않음) |
| 조각에 `[1]..[k]` 를 붙여 LLM 에 넘김. LLM 이 `NO_ANSWER` | `no_result` |
| LLM 시간 초과 / 그 밖의 오류 | `failed` + `llm_timeout` / `llm_failed` |
| 답에 `[n]` 이 없거나 넘긴 번호 밖 | `failed` + `llm_failed` (근거를 지어냈을 수 있음) |
| 통과 | `answered`. `citations` 는 답에 나온 번호의 조각만 (`ref`, `docId`, `sourceType`, `title`, `text` 원문, `location`) |

- answered·no_result·failed 모두 200 이고, 입력 오류는 400 `INVALID_INPUT`, DB·임베딩 모델·LLM 설정이 준비되지 않으면 503 `NOT_READY`
- 지시문(시스템 프롬프트)은 `rag/prompts/answer/v1.md`, `v2.md` … 처럼 버전마다 파일이 따로 있다. `RAG_ANSWER_PROMPT_VERSION`(예: `v1`)으로 고르고, 없으면 번호가 가장 큰 파일을 쓴다. 쓰는 버전은 로그에 `prompt=answer-v2` 처럼 남는다. 새 버전 만드는 법은 `rag/prompts/README.md`
- 조각 글은 프롬프트에서 `<자료>` 칸에 넣고, 그 안의 지시는 따르지 말라고 적는다
- 임베딩 모델은 서버가 한 번 불러 작업자와 같이 쓴다 (잠금으로 한 번에 하나씩). 문서를 임베딩하는 중에 질문이 오면 그 묶음이 끝날 때까지 기다린다
- 로그에는 질문·답·조각 글을 남기지 않는다 (요청 ID, 개수, 상태, 오류 코드, 검색·LLM 걸린 ms, LLM 공급자·모델, 입력·출력 토큰 수). 출력 토큰에는 추론 토큰을 더한다 (출력 요금 기준)
- 실제 LLM 을 쓰면 검색된 조각이 공급자(Google·OpenAI)로 전송된다. 실제 회의 데이터를 보내도 되는지는 서비스 정책으로 정한다
- 유사도 하한은 아직 없다 (측정 전). 관련 없는 조각만 걸리면 LLM 의 `NO_ANSWER` 로 거른다

손으로 시험하기: 서버를 띄운 뒤 다른 터미널에서 대화형 도구를 쓴다. 회의록을 시험용 워크스페이스(999999)에 넣고, 처리가 끝나면 질문을 받아 답과 근거를 보여 주고, 끝날 때 지운다 (`--keep` 이면 남김).

```bash
.venv/bin/python -m rag.try_answer                                        # 합성 예시 회의록으로
.venv/bin/python -m rag.try_answer --file a.json --title "첫 회의" --file b.json --title "후속 회의"   # 여러 문서
```

- 가짜 LLM 은 검색된 조각을 모두 근거로 단다. 어느 문서의 어떤 조각이 찾아졌는지 보는 용도다 (`--top-k` 로 개수 조정)
- 실제 LLM 으로 보려면 서버를 띄울 때 `RAG_LLM_PROVIDER=gemini`, `RAG_LLM_MODEL`, `GEMINI_API_KEY` 를 넣는다 (`rag/.env` 에 적어 두면 된다). 질문마다 요금이 나간다
- 실제 회의 데이터는 레포 밖(또는 git 이 무시하는 `data/`)에 두고 `--file` 로 경로만 넘긴다

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
| 영구 실패 | `no_text`, 형식 오류, 업로드 받기 실패·만료 등은 재시도 없이 `failed` 콜백. 업로드 문서는 로더가 생길 때까지 `unsupported_format` (`last_error` 에 "업로드 로더 미구현 (형식)") |
| 일시 실패 | 그 밖의 예외는 30초·2분 뒤 다시, 3번째도 실패하면 `internal_error` |
| 결과 버림 | 처리 중 문서가 지워졌거나 더 새 버전이 접수되면 콜백 없이 끝낸다 |
| 콜백 | `POST {callbackUrl}`, `X-Request-Id` 를 돌려준다. 408·429·5xx·연결 오류만 3번 재시도(1·2·4초), 리다이렉트는 따라가지 않는다 |
| 정리 | 끝난 작업은 `payload`(문서 내용)를 비우고, 7일 뒤 줄을 지운다 |
| DB 접속 | 시작할 때 접속하지 않고 반복을 돌며 접속한다. DB 가 늦게 뜨거나 `vector` 확장이 없어도 서버는 뜨고(그동안 RAG 주소는 503), 연결이 끊기면 다음 반복에서 다시 접속한다 |

## 검색 품질 평가

질문마다 "찾아와야 할 정답 청크"를 정해 둔 질문 파일로, 정답 청크가 검색 결과 몇 위에 오는지 보고 Hit@k·Recall@k·MRR 을 유형별로 낸다. AI 를 부르지 않아 비용이 없고 회의록이 밖으로 나가지 않는다.

```bash
.venv/bin/python -m rag.eval_search data/eval/questions_v1.json                    # Top-5, Top-10
.venv/bin/python -m rag.eval_search data/eval/questions_v1.json --k 5 10 20 --detail --out data/eval/results/x.json
```

- 청크는 실제 작업자와 같은 함수(로더 → 나누기 → 임베딩)로 만들고, 서버 검색과 같은 모델·기준(코사인)으로 순위를 낸다. DB 를 쓰지 않는다
- 질문 파일 형식은 `rag/eval_search.py` 맨 위 설명. `gold` 가 빈 질문(답할 수 없음)은 검색 지표에서 뺀다
- 회의록·질문·결과는 레포 밖이나 git 이 무시하는 `data/` 에 둔다. 실제 회의 데이터로 낸 결과는 올리지 않는다

## 이미지·배포

공용 `Dockerfile` 이미지에 RAG 가 함께 들어간다. GitHub Actions(`.github/workflows/image.yml`)가 PR 에서는 빌드·크기 측정·모델 확인만 하고, `develop` 에 합쳐지면 `ghcr.io/nineouting/nineouting-ai-service:develop` 과 `:<커밋 SHA>` 로 올린다.

- torch 는 CPU 전용, 임베딩 모델은 빌드 때 `/models` 에 받아 sha256 을 확인하고 실행 중에는 내려받지 않는다(`HF_HUB_OFFLINE=1`)
- root 가 아닌 사용자(uid 10001)로 실행
- `linux/arm64`(CN팀 노드)와 `linux/amd64`(테스트용)를 각자의 빌드 서버에서 만들어 하나의 태그로 묶는다. 받는 쪽 CPU 에 맞는 이미지가 자동으로 받아진다 (`docker pull --platform linux/amd64 …` 로 고를 수 있음)
- GHCR 의 옛 이미지는 자동으로 지우지 않는다. 묶음 안쪽 플랫폼 이미지가 "태그 없는 버전"으로 보여, 태그 없는 버전을 지우면 묶음이 깨진다

처음 배포할 때 (운영 DB)
1. 관리자가 `CREATE EXTENSION vector` (앱 계정은 관리자 권한이 없음)
2. 같은 이미지로 `python -m rag.migrate` 를 한 번 (`RAG_DATABASE_URL` 필요). 새 마이그레이션이 생기면 배포 전에 다시
3. 서버에 `RAG_DATABASE_URL`, `RAG_CALLBACK_ALLOWED_HOSTS` 를 넣고 띄운다. `RAG_DATABASE_URL` 이 없으면 RAG 는 꺼진 채 요약만 뜬다

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
| `RAG_UPLOAD_ALLOWED_HOSTS` | 업로드 문서를 받을 저장소 호스트 (S3 버킷 주소 등, 쉼표로). **비어 있으면 업로드 문서를 받지 않음** (콜백과 달리 모두 허용이 없음) |
| `RAG_UPLOAD_MAX_MB` | 업로드 파일 최대 크기(MB). 정해 둔 값이며 SCRUM-110 에서 재서 조정. 기본 20 |
| `RAG_UPLOAD_TIMEOUT_S` | 파일 받기 시간 제한(초). 기본 30 |
| `RAG_TORCH_THREADS` | 임베딩 스레드 수 (작업자·답변이 같이 씀) (기본 2). 같은 노드의 STT 와 CPU 를 나눠 쓰려고 |
| `RAG_DB_STATEMENT_TIMEOUT_MS`, `RAG_DB_IDLE_TX_TIMEOUT_MS` | 작업자 DB 연결의 시간 제한 (기본 30000, 60000) |
| `RAG_LLM_PROVIDER` | 답변 LLM 공급자: `fake`(기본, 시험용 가짜) / `gemini`(시험) / `openai`(운영). 설정이 잘못되면 서버는 뜨고 답변만 503 |
| `RAG_LLM_MODEL` | 공급자의 모델 이름. `gemini`·`openai` 는 꼭 넣는다 (기본값 없음) |
| `RAG_ANSWER_PROMPT_VERSION` | 답변 지시문 버전 (`v1`, `v2` …). 없으면 `rag/prompts/answer/` 에서 번호가 가장 큰 파일. 없는 버전이면 답변만 503 |
| `RAG_LLM_TIMEOUT_S` | 답변 LLM 을 기다리는 상한 (기본 30초). 넘으면 `llm_timeout`. 재시도는 하지 않는다 |
| `GEMINI_API_KEY`(또는 `GOOGLE_API_KEY`), `OPENAI_API_KEY` | 공급자 API 키. 요약 서버와 같은 이름이라 하나로 같이 쓴다. 이미지에 넣지 않고 실행할 때 비밀값으로 |
| `RAG_DB_CONNECT_TIMEOUT_S` | 작업자가 DB 에 접속할 때 기다리는 상한 (기본 10초). 닿지 않는 주소에서 서버 종료가 늦어지지 않게 |
| `HF_HUB_CACHE` | 모델 캐시 위치. 없으면 `~/.cache/huggingface/hub`. 이미지에서는 `/models` 로 고정 (실행 사용자가 바뀌어도 같은 곳을 찾도록) |
