# rag

RAG 파이프라인 (문서 넣기·검색·답변). 작업 규칙은 `AGENTS.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 회의록 로더, 256토큰 나누기, 임베딩 | 구현. 확정 회의록(`formatVersion` 1.0, 칸의 줄 하나·표의 행 하나·머리글·바닥글이 각각 조각 하나)과 임시 형식(`schema_version` 0.1)을 받음 |
| 저장(pgvector, 버전 교체), 검색(workspaceId + 문서 짝 docs) | 구현. 문서는 `sourceType`+`docId` 짝으로 구분 (SCRUM-227, 챗봇 명세 v0.2 9장) |
| 대기열·작업자, 완료 콜백 | 구현 |
| 내부 API (문서 넣기·상태·삭제) | 구현. 회의록(`sourceType=minutes`)만 받음 |
| 배포 이미지 (`ghcr.io/nineouting/nineouting-ai-service`) | 구현. 아래 "이미지·배포" |
| 답변 API (검색 → LLM → 근거 번호 검사) | 구현. LLM 은 설정으로 고름: 가짜(기본, 답 앞에 `[시험용 답변]`) / Gemini(시험) / OpenAI(운영, SDK 는 모델을 정할 때 추가) |
| 업로드 문서(PDF·워드·엑셀) | 받기·형식 확인(SCRUM-106), PDF 읽기(SCRUM-107, PDFium), 워드 읽기(SCRUM-108), 엑셀 읽기(SCRUM-109) 구현. 정제·크기 상한은 SCRUM-110 |
| 회의 중 챗봇 (`POST /internal/rag/chat`) | 이어지는 질문 + 회의록·첨부 자료로 답하기 (SCRUM-223), 진행 중 회의(안건 보드·최근 전사 줄, SCRUM-224) 구현. 일반 지식(SCRUM-225)은 아직. 명세 `docs/api/rag-chat-contract.md` |

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
cp .env.example .env   # 저장소 루트에서. "RAG 답변" 절의 주석을 풀고 값을 채운다. 채팅·로그에 붙여 넣지 않는다. 로컬 전용이라 간단한 값이면 된다
```

- 설정 파일은 저장소 루트 `.env` 하나다. 요약·RAG·회의록 초안 설정이 같이 들어간다
- 예전에 `rag/.env` 를 만들어 썼다면 그 줄들을 루트 `.env` 로 옮긴다 (루트 `.env` 가 아직 없으면 `cp rag/.env .env`). 옮기는 동안 손 시험·평가 도구는 `rag/.env` 도 읽고, 거기서 값을 가져오면 경고한다 (루트 `.env` 가 우선)

### A. Homebrew (macOS, 상주 서비스 없이)

`.env` 의 포트는 `5432` (기본값 그대로).

```bash
brew install postgresql@17 pgvector     # pgvector 0.8 이상인지 brew info pgvector 로 확인

PG=/opt/homebrew/opt/postgresql@17/bin
DATA=/opt/homebrew/var/postgresql@17
$PG/pg_ctl -D $DATA -l /opt/homebrew/var/log/postgresql@17.log -w start   # 띄우기
$PG/pg_ctl -D $DATA stop                                                  # 끄기

# 처음 한 번: 앱 계정·DB·vector 확장 만들기 (비밀번호는 .env 에서 읽는다)
set -a; . ./.env; set +a
$PG/psql -X -d postgres -v ON_ERROR_STOP=1 -f rag/db/init/01_roles.sql
```

- `brew services start` 를 쓰지 않으면 로그인할 때 자동으로 뜨지 않는다. 필요할 때만 띄운다
- Homebrew 기본 설정은 이 컴퓨터 안의 접속을 비밀번호 없이 허용한다(`trust`). 로컬에서는 비밀번호가 검사되지 않는다

### B. Docker

`.env` 의 두 주소 포트를 `5433` 으로 바꾸고 `POSTGRES_PASSWORD` 도 채운다.

```bash
cd rag && docker compose --env-file ../.env up -d && cd ..   # pgvector/pgvector:0.8.0-pg17, 포트 127.0.0.1:5433
cd rag && docker compose --env-file ../.env down && cd ..    # 끄기 (데이터는 볼륨에 남음)
```

- 처음 띄울 때 `db/init/01_roles.sql` 이 자동으로 실행된다 (볼륨이 비어 있을 때만. 비밀번호를 바꾸려면 볼륨을 지우고 다시 띄운다)

### 마이그레이션 (공통)

```bash
set -a; . ./.env; set +a                 # 환경변수로 불러오기 (현재 셸에서만)
.venv/bin/python -m rag.migrate           # 개발 DB(rag)
.venv/bin/python -m rag.migrate --test    # 시험 DB(rag_test)
```

- 앱 계정 `rag_app` 은 관리자 권한이 없다 (운영의 전용 계정과 같은 조건)

## 내부 API (백엔드 → AI 서버)

`server/main.py` 에 라우터로 붙는다. `RAG_DATABASE_URL` 이 있을 때만 붙고, 없으면 요약만 뜬다.

**아래는 모두 같은 터미널에서 한다.** `.env` 를 불러오지 않은 터미널에서 서버를 띄우면 RAG 가 꺼진 채로 떠서 RAG 주소가 `404 Not Found` 가 된다.

```bash
cd <저장소 루트> && set -a && . ./.env && set +a
.venv/bin/pip install -r requirements.txt       # 요약 서버 의존성 (처음 한 번)
# DB 띄우기 (Homebrew 기준, 위 "DB" 참고). pg_ctl 은 전체 경로로 쓴다
/opt/homebrew/opt/postgresql@17/bin/pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/log/postgresql@17.log start
.venv/bin/python -m rag.migrate                 # 새 마이그레이션이 있을 때마다
.venv/bin/uvicorn server.main:app --port 8000   # 로그에 "RAG 작업자 시작" 이 나오면 준비 끝
```

- 루트 `.env` 에는 요약 설정도 들어 있어서 같이 불러와진다. `LLM_PROVIDER` 를 실제 공급자로 두면 로컬 서버의 회의 중 요약도 실제 AI 를 부른다 (요금이 나간다)
- 브라우저로 http://127.0.0.1:8000/docs 를 열면 주소별 설명·필드·예시가 있고, "Try it out" 으로 직접 호출할 수 있다
- `RAG_WORKER_IN_PROCESS=0` 이면 작업자는 띄우지 않는다 (`python -m rag.worker` 로 따로)

| 주소 | 응답 |
|---|---|
| `POST /internal/rag/documents` | 202 `{sourceType, docId, docVersion, status: "accepted"}`. 저장된 것보다 낮은 회의록 버전이면 `"ignored"` (처리·콜백 없음) |
| `GET /internal/rag/documents/{sourceType}/{docId}` | 200 `{sourceType, docId, docVersion, status, errorCode, chunkCount}`, 없으면 404 |
| `DELETE /internal/rag/documents/{sourceType}/{docId}` | 204 (없어도) |
| `DELETE /internal/rag/workspaces/{workspaceId}` | 204 |

- 문서는 `sourceType`(`minutes` 회의록 / `upload` 첨부 자료)과 `docId` 의 짝으로 구분한다. 회의록 200번과 첨부 자료 200번은 다른 문서다. 완료 콜백에도 `sourceType` 이 들어간다 (`{sourceType, docId, docVersion, status, errorCode, chunkCount}`)
- 옛 모양(`/documents/{docId}`, 답변의 `docIds`)은 받지 않는다 (2026-11-12, SCRUM-227). 번호만으로는 종류를 알 수 없다. 옛 주소는 404, `docIds` 만 보내면 400
- 상태 값은 `accepted` / `processing` / `ready` / `failed` (DB 의 `queued` 를 `accepted` 로 보여 줌)
- 명세에 없어서 AI 쪽이 정한 것 (확인 필요): 입력 오류 400 `{"error": "INVALID_INPUT"}`, 같은 `sourceType`+`docId` 가 다른 워크스페이스로 오면 409 `CONFLICT`, DB 를 쓸 수 없으면 503 `NOT_READY`, 넣기 응답 코드 202
- `sourceType=upload` 는 허용 저장소(`RAG_UPLOAD_ALLOWED_HOSTS`)가 설정됐을 때만 받는다. 없으면 400. `fileUrl` 의 호스트가 허용 저장소가 아니어도 400
  - 작업자가 파일을 메모리로 받아(디스크에 쓰지 않음) 안쪽 모양으로 형식을 알아낸다. 확장자는 믿지 않는다
  - 실패: 서명 만료 `url_expired`, 그 밖에 받지 못함 `download_failed` (둘 다 다시 시도하지 않음. 백엔드가 다시 호출), 크기·푼 크기 초과와 조각 토큰 합계 초과(`RAG_MAX_DOC_TOKENS`) `too_large`, 암호 `encrypted`, pdf·docx·xlsx 가 아님(사용자가 묶은 zip, 옛 doc·xls, hwp 포함) `unsupported_format`
  - PDF: 쪽마다 머리글·바닥글(여러 쪽의 위·아래 두 줄 안에 되풀이되는 줄)을 지우고, 쪽 안에서 번호 제목(`1.`, `3.2`)마다 조각 재료 하나. 검색 맥락은 제목 경로, 쪽 첫 부분은 앞 쪽 제목을 이어받음. 번호 없는 제목은 나누지 못함. 근거 위치 `{type: "page", page, label: "3쪽"}`. 글 없는 쪽은 건너뛰고, 모든 쪽에 글이 없으면(스캔 PDF 등) `no_text`, 1,000쪽 넘으면 `too_large`. PDFium(`pypdfium2`)으로 읽는다. pypdf 는 글자 폭 정보가 어긋난 PDF 에서 글자마다 띄어 꺼내서 쓰지 않는다
  - 워드: 제목 하나 아래의 글이 조각 재료 하나. 근거 위치 `{type: "section", heading, label}`(가장 가까운 제목, 첫 제목 전 글은 label "문서 처음"), 검색 맥락은 제목 경로("2. 예산 계획 > 2.1 마케팅"). 표는 행마다 "머리 행 이름: 값 | …" 한 줄. 머리글·바닥글은 본문과 따로 저장돼 읽지 않는다. 글상자·각주·자동 번호는 읽지 못한다
  - 엑셀: 시트마다 처음으로 글이 있는 행을 머리 행으로 보고, 그 아래 행 하나가 조각 재료 하나. 행은 "열 이름: 값 | …" 한 줄. 근거 위치 `{type: "sheet", sheet, rows: [시작, 끝], label: "1분기 시트 12행"}`(행 번호는 엑셀 화면 그대로), 검색 맥락은 시트 이름. 병합 칸은 범위 전체에 채우고, 숫자는 쉼표·백분율·날짜 서식을 따름. 수식은 저장된 계산 결과만(없으면 빠짐). 숨긴 시트는 읽지 않음. 칸 20만 개 넘으면 `too_large`. 차트·그림·칸 메모는 읽지 못하고, 한 시트에 표가 여럿이면 첫 행만 머리 행으로 봄
  - 머리글·바닥글·쪽 번호 빼기 같은 정제는 아직 없다 (SCRUM-110)
  - 시험 샘플(합성): `rag/tests/fixtures/upload_*.pdf` (짧은 보고서, 상세 운영계획서 `upload_plan.pdf`, 글 없는 PDF, 암호 PDF), 만드는 스크립트 `make_upload_samples.py` (fpdf2, Noto Sans CJK KR, 운영계획서 내용은 `plan_content.py`)
  - `fileUrl` 에는 접근 서명이 들어 있어 로그·오류 글에 남기지 않는다
- `minutes` 형식 (회의록 초안 명세 `docs/api/minutes-draft-contract.md` 4장)
  - 확정 회의록 `formatVersion: "1.0"`: `meetingId`, `meetingStartAt`, `savedAt`(선택), `header`·`footer`(선택), `sections[{title, content}]`. `content` 의 줄 하나가 조각 하나이고 줄 앞 `- `·`* `·`• ` 는 뗀다. 들여 쓴 목록은 하위 줄에 위 줄들을 `: ` 로 붙여 조각 하나로 넣고 하위 줄이 있는 위 줄은 따로 넣지 않는다 (초안이 안건 등으로 묶은 필드). 빈 줄과 "해당 내용 없음" 은 넣지 않는다. `content` 안의 마크다운 표(`|` 로 시작하고 둘째 줄이 `|---|`)는 행마다 조각 하나로, 워드 표처럼 "머리 행 이름: 값 | …" (빈 칸은 뺌, 칸 안의 `\|` 는 구분이 아님). 칸 이름을 맥락으로 붙여 검색하고, 근거 위치는 `{type: "section", heading, label}` (칸 이름. 워드 문서의 제목 위치와 같은 모양). 머리글·바닥글은 글이 있으면 각각 조각 하나이고 heading 은 "머리글"·"바닥글"
  - 임시 형식 `schema_version: "0.1"` (summary.json): 전환 기간에만. 근거 위치는 녹음 시간 `{type: "time", startMs, endMs, label}`
  - 둘 다 아니거나 확정 회의록 모양이 틀리면 400 (어느 칸이 틀렸는지 알려 줌)
- 회의록 버전이 저장된 것과 새 것 모두 정수 모양(`"5"`)이면 숫자로 비교해 낮은 버전은 무시한다. 재시도로 옛 확정본이 늦게 와서 새 확정본을 덮어쓰지 않게 하려는 것. 정수 모양이 아니면 마지막에 받은 것을 쓴다
- 서버 간 인증은 지금 없다. 내부망으로만 연다 (백엔드와 합의, 2026-11-12). 서비스 간 토큰은 보안 점검의 "내부 주소 인증" 과제 때 백엔드·인프라와 정하고, 그때 `api.verify_internal_request` 와 `shared.callback.auth_headers` 를 채운다

### 답변 `POST /internal/rag/answer`

요청 `{workspaceId, docs: [{sourceType, docId}], query, topK?}` (`docs` 최대 1,000개, 같은 짝은 하나로. ID 는 모두 정수, topK 1~20 기본 5, 그 밖의 필드는 무시) → 200 `{status, answer, citations, errorCode}`

| 단계 | 결과 |
|---|---|
| `docs` 가 비었음 | `no_result` (DB·LLM 을 부르지 않음) |
| `workspaceId`·`docs` 로 거른 검색에서 조각 0개 | `no_result` (LLM 을 부르지 않음) |
| 조각에 `[1]..[k]` 를 붙여 LLM 에 넘김. LLM 이 `NO_ANSWER` | `no_result` |
| LLM 시간 초과 / 그 밖의 오류 | 폴백 모델이 있으면 폴백으로 한 번 더. 그래도 실패하면 `failed` + `llm_timeout` / `llm_failed` |
| 답에 `[n]` 이 하나도 없음 | `no_result` (근거 없는 글은 보여 주지 않음. AI 가 `NO_ANSWER` 대신 "자료에 없습니다" 같은 문장을 쓰는 경우) |
| 답에 넘긴 번호 밖의 `[n]` 이 있음 | `failed` + `llm_failed` (근거를 지어냈을 수 있음) |
| 통과 | `answered`. `citations` 는 답에 나온 번호의 조각만 (`ref`, `docId`, `sourceType`, `title`, `text` 원문, `location`) |

- answered·no_result·failed 모두 200 이고, 입력 오류는 400 `INVALID_INPUT`, DB·임베딩 모델·LLM 설정이 준비되지 않으면 503 `NOT_READY`
- 지시문(시스템 프롬프트)은 `rag/prompts/answer/v1.md`, `v2.md` … 처럼 버전마다 파일이 따로 있다. `RAG_ANSWER_PROMPT_VERSION`(예: `v1`)으로 고르고, 없으면 번호가 가장 큰 파일을 쓴다. 쓰는 버전은 로그에 `prompt=answer-v2` 처럼 남는다. 새 버전 만드는 법은 `rag/prompts/README.md`
- 조각 글은 프롬프트에서 `<자료>` 칸에 넣고, 그 안의 지시는 따르지 말라고 적는다
- 조각 머리는 `[n] (제목 · 날짜, 위치)` 이다 (SCRUM-234). 날짜는 회의록이면 `회의 2026년 10월 29일 10:00`(넣을 때 받은 `meetingStartAt` 그대로, 시간대는 떼고 벽시계 시각), 첨부 자료면 `올린 날 2026년 10월 6일`(AI 서버가 그 버전을 받은 시각, 한국 날짜). 날짜가 없으면(날짜 저장 전에 넣은 회의록) 붙이지 않는다. API 응답은 바뀌지 않는다
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
- 실제 LLM 으로 보려면 서버를 띄울 때 `RAG_LLM_PROVIDER=openai`, `RAG_LLM_MODEL=gpt-5.6-luna`, `OPENAI_API_KEY` 를 넣는다 (`.env` 에 적어 두면 된다). 질문마다 요금이 나간다
- 실제 회의 데이터는 레포 밖(또는 git 이 무시하는 `data/`)에 두고 `--file` 로 경로만 넘긴다

### 회의 중 챗봇 `POST /internal/rag/chat`

명세는 `docs/api/rag-chat-contract.md`. 지금(SCRUM-223)은 대화를 이어받아 회의록·첨부 자료로만 답한다.

```
① history 가 있으면 질문 고쳐 쓰기 (AI 1번)   "그거 언제까지야?" → "디자인 시안 수정은 언제까지야?"
② 근거 모으기 (AI 없음)                       고친 질문으로 workspaceId + docs 짝 검색, 상위 5개
③ [1]..[n] 번호 붙이기
④ 답하기 (AI 1번)                             <대화> + <자료> + <질문(원래 질문)> + <고친 질문(다를 때만)>
⑤ 판정                                        답변 API 와 같은 judge_reply
```

| 단계 | 결과 |
|---|---|
| `docs` 가 비었고 진행 중 회의 근거도 없음 | `no_result` (DB·AI 를 부르지 않음. 일반 지식이 생기면(SCRUM-225) 바뀐다). `docs` 만 비면 검색을 건너뛰고 진행 중 회의로 답한다 |
| 고쳐 쓰기 실패 (오류, 시간 초과, 빈 답, `NO_ANSWER`, 여러 줄, 300자 넘음) | 원래 질문으로 이어 간다. 답은 실패시키지 않는다 |
| 60초 마감을 넘김 | `failed` + `llm_timeout` (그래도 200) |
| 찾은 근거 0개 / AI 가 `NO_ANSWER` / 답에 `[n]` 없음 | `no_result`, `answer` 는 "회의 자료에서 찾을 수 없어요" (답변 API 는 `null`) |
| 넘긴 번호 밖의 `[n]` / AI 오류·시간 초과 | `failed` + `llm_failed` / `llm_timeout` |
| 통과 | `answered`, `basis` 는 지금 늘 `sources` |

- 대화 기록은 저장하지 않는다. 백엔드가 요청마다 `history`(최대 20개)로 보내고, 고쳐 쓰기·답하기에는 최근 6개만 쓴다. 앞선 챗봇 답의 `[n]`·`[일반]` 표시는 지우고 넘긴다 (그때의 근거 번호라 이번 번호와 헷갈린다)
- `<대화>`는 질문이 가리키는 것을 알아내는 데만 쓰고 근거로 쓰지 않는다. 앞선 챗봇 답에만 있고 자료에 없는 사실은 답하지 않는다
- 진행 중 회의 (`rag/live.py`, 명세 4.2): 안건 보드의 결정할 사항·요약·의견·할 일 하나가 근거 하나 (논의 전 사항은 뺌). 전사 줄은 맞장구·간투사·확신도 낮은 줄을 빼고 5줄 또는 400자씩 묶는다. 상한(보드 6,000자, 전사 줄 4,000자) 안이면 모두 넘기고, 넘으면 임베딩 없이 정해 둔 순서(지금 안건 → 결정 → 할 일 → 요약 → 의견, 전사 줄은 최근 것부터)로 고른다
- 근거 머리: 진행 중 회의는 `[n] (진행 중 회의 · 지금 진행 중 · 지금 안건 · 질문한 사용자의 할 일, 디자인 시안 수정 안건의 할 일)` 처럼 표시를 붙인다. 표시는 AI 에만 넘기고 `citations[].text` 에는 넣지 않는다. 담당자가 `userId` 와 같으면 "질문한 사용자의 할 일", `unclear` 이면 "할 일인지 확인 필요"
- `userName` 이 있으면 `<질문한 사람>` 칸으로 넘긴다 (지난 회의록의 담당자 이름과 맞추는 데). 로그에는 남기지 않는다
- `liveAgendas`·`recentLines` 는 쓰는 칸의 타입을 검사한다 (명세 3.4·3.5). 상태·종류 값은 검사하지 않고, 모르는 결정할 사항 상태는 넘기지 않는다
- 시간 (명세 v0.2 4.4): 요청 하나는 최대 60초. 고쳐 쓰기는 기본 모델만 최대 5초(폴백 없음), 답하기는 기본 모델 최대 25초(남은 시간보다 길면 남은 시간까지), 실패하면 남은 시간이 10초 이상일 때만 폴백 모델을 남은 시간 안에서 부른다. 백엔드는 이 주소만 타임아웃 65초 (권장)
- 고쳐 쓰기와 답하기는 답변 API 와 같은 AI 설정(`RAG_LLM_*`)을 쓰되 연결은 따로 만든다. 위 시간 규칙 때문에 공용 폴백 묶음을 그대로 부르지 않고 기본·폴백 모델을 꺼내 직접 부른다. 그래서 "실패한 기본 모델을 60초 동안 건너뛰기"(`RAG_LLM_FALLBACK_COOLDOWN_S`)는 챗봇에 없다. 장애 중에는 질문마다 기본 모델을 25초까지 기다린 뒤 폴백으로 간다 (측정 뒤 넣을지 정함). `RAG_LLM_TIMEOUT_S` 도 챗봇에 쓰지 않는다
- 문서는 `docs: [{sourceType, docId}]` 짝으로 받는다 (최대 1,000개, 답변 API 와 같음). 옛 `docIds` 는 400
- 상한: 대화 한 개의 글 4,000자, `userName` 100자 (명세 v0.3 3.1)
- 로그: `챗봇 answered request_id=… workspace=… meeting=… user=… history=… docs=… live_agendas=… recent_lines=… live_board=… live_lines=… rewrite=ok rewrite_ms=… docs_found=… evidence=… llm_ms=… tokens_in=…`. `rewrite` 는 `ok` / `skip`(대화 없음) / `fallback_timeout` / `fallback_error` / `fallback_invalid`. 질문·대화·답·근거 글과 `userName` 은 남기지 않는다
- 가짜 AI 는 고쳐 쓰기에서 `NO_ANSWER` 를 내므로 늘 `rewrite=fallback_invalid` 로 원래 질문을 쓴다

손으로 시험하기: 서버를 띄운 뒤 다른 터미널에서. 대화 기록은 도구가 들고 있다가 질문마다 보낸다 (`/new` 새 대화, `/history` 기록 보기).

```bash
.venv/bin/python -m rag.try_chat                                          # 합성 예시 회의록으로
.venv/bin/python -m rag.try_chat --file rag/tests/fixtures/minutes_confirmed_table.json --title "주간 회의 (10/20)" \
    --file rag/tests/fixtures/answer_minutes_followup.json --title "주간 회의 (10/27)"
.venv/bin/python -m rag.try_chat --show-rewrite                           # 고친 질문도 본다 (AI 를 한 번 더 부름)
```

## 작업자

접수(`jobs.submit()`)는 문서를 `queued` 로 두고 작업 표(`rag_jobs`)에 적기만 한다. 작업자가 꺼내서 로더 → 나누기 → 임베딩 → 저장을 하고 완료 콜백을 보낸다.

```bash
set -a; . ./.env; set +a
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
.venv/bin/python -m rag.eval_search rag/tests/fixtures/eval_plan.json --format pdf --k 1 3 5   # 업로드 문서 (합성), pdf·docx·xlsx
```

- 업로드 문서 질문 파일 `eval_plan.json` 은 합성 운영계획서(pdf·docx·xlsx 같은 내용) 질문 40개다. 정답은 위치(쪽, 제목, 시트·행)로 적는다

- 청크는 실제 작업자와 같은 함수(로더 → 나누기 → 임베딩)로 만들고, 서버 검색과 같은 모델·기준(코사인)으로 순위를 낸다. DB 를 쓰지 않는다
- 질문 파일 형식은 `rag/eval_search.py` 맨 위 설명. `gold` 가 빈 질문(답할 수 없음)은 검색 지표에서 뺀다
- 회의록·질문·결과는 레포 밖이나 git 이 무시하는 `data/` 에 둔다. 실제 회의 데이터로 낸 결과는 올리지 않는다

## 답변 지시문 평가

질문마다 넘길 조각을 정해 두고 실제 AI 로 여러 번 답하게 해, 정답표(상태, 꼭 쓸 근거, 꼭 들어갈 말, 따르면 안 되는 지시)와 맞는 횟수를 세고 지시문 버전끼리 비교한다 (`eval_answer.py`). 검색은 하지 않으므로 지시문 차이만 본다.
사례 파일은 `rag/tests/fixtures/answer_eval_*.json` 에 둔다. 형식은 `eval_answer.py` 맨 위 설명.

```bash
.venv/bin/python -m rag.eval_answer rag/tests/fixtures/answer_eval_two_meetings.json --dry-run      # AI 없이 조각·정답표 점검
.venv/bin/python -m rag.eval_answer rag/tests/fixtures/answer_eval_two_meetings.json --runs 20      # 실제 AI. 호출마다 요금이 나간다
.venv/bin/python -m rag.eval_answer 사례.json --runs 20 --prompt v3 v4                               # 버전을 번갈아 비교
.venv/bin/python -m rag.eval_answer 사례.json --rescore data/eval/answer/{결과폴더}                  # 정답표를 고친 뒤 저장된 답을 다시 채점
```

| 사례 | 내용 |
|---|---|
| `answer_eval_basic.json` | 회의 하나, 6문항: 결정 날짜, 사람 이름만, 사용자가 만든 표 속 금액, 메모 속 장소, 자료에 없는 질문 2개 |
| `answer_eval_two_meetings.json` | 회의 둘(10/20, 10/27), 5문항: 두 회의에 걸친 변경, 맡은 일이 여러 개인 사람, 일부만 답할 수 있는 질문, 지시문이 섞인 조각(답이 있는/없는 질문) |

- AI 설정은 서버와 같다 (`RAG_LLM_*`, 환경변수가 없으면 `.env`). 조각 글과 판정은 답변 API 와 같은 함수(`build_prompt`, `judge_reply`)를 쓴다
- 결과는 `data/eval/answer/{사례}-{시각}/` 에 남는다 (git 이 무시함). 답 글이 들어가므로 합성 회의록만 쓴다
- 꼭 들어갈 말은 낱말로 본다. 틀림으로 나온 답은 `runs.jsonl` 의 글을 직접 보고 판단한다 (같은 뜻을 다르게 쓴 경우가 있다)

## 챗봇 평가

대화가 이어지는 질문에서 질문을 고쳐 쓰기 전·후의 검색 순위(Hit@5, MRR)와 답이 맞는지를 잰다 (`eval_chat.py`). 검색은 `eval_search` 처럼 실제 임베딩 모델로 메모리에서 하고, 고쳐 쓰기·답하기는 서버와 같은 함수(`rewrite_query`, `chat_input`, `judge_reply`)를 쓴다. 사례 형식은 `eval_chat.py` 맨 위 설명.

```bash
.venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --dry-run     # AI 없이 점검, 고치기 전 순위
.venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_two_meetings.json --runs 10     # 실제 AI. 호출마다 요금이 나간다
.venv/bin/python -m rag.eval_chat --summary data/eval/chat/{결과폴더}
.venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_live.json --dry-run          # 진행 중 회의 (SCRUM-224)
.venv/bin/python -m rag.eval_chat rag/tests/fixtures/chat_eval_live_long.json --dry-run     # 넘치는 진행 중 회의
```

- `chat_eval_two_meetings.json`: 답변 평가와 같은 합성 회의 둘(10/20, 10/27), 12사례. 앞 답·사람·장소·목록 속 항목을 가리키는 질문, 생략된 주제, 주제가 바뀐 질문, 대화 없는 질문, 앞선 챗봇 답에만 있는 사실(→ `no_result`), 대화에 섞인 지시
- 조각이 19개뿐이라 고쳐 쓰지 않아도 정답이 상위에 오는 사례가 많다. 고치기 전·후 차이는 조각이 많은 자료에서 더 크게 날 수 있다
- 결과는 `data/eval/chat/{사례}-{시각}/` 에 남는다 (git 이 무시함). 고친 질문과 답 글이 들어가므로 합성 회의록만 쓴다. 폴백은 쓰지 않는다

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
set -a; . ./.env; set +a                 # DB 시험을 돌리려면 (없으면 DB 시험은 건너뜀)
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
| `RAG_UPLOAD_MAX_MB` | 업로드 파일 최대 크기(MB). 백엔드의 사전 자료 상한과 같게 둔다. 이보다 작은 파일도 글이 많으면 `RAG_MAX_DOC_TOKENS` 로 `too_large` 가 난다. 기본 50 |
| `RAG_UPLOAD_TIMEOUT_S` | 파일 받기 시간 제한(초). 기본 30 |
| `RAG_MAX_DOC_TOKENS` | 문서 하나의 조각 토큰 합계 상한. 넘으면 임베딩 전에 `too_large`. 임베딩 시간이 토큰 수에 비례해서 둔다 (기본 100000, 합성 문서로 PDF 약 145쪽·엑셀 약 2,000행). 처리 완료 로그의 `tokens`·`걸린시간`으로 서버 속도를 보고 조정 |
| `RAG_TORCH_THREADS` | 임베딩 스레드 수 (작업자·답변이 같이 씀) (기본 2). 같은 노드의 STT 와 CPU 를 나눠 쓰려고 |
| `RAG_DB_STATEMENT_TIMEOUT_MS`, `RAG_DB_IDLE_TX_TIMEOUT_MS` | 작업자 DB 연결의 시간 제한 (기본 30000, 60000) |
| `RAG_LLM_PROVIDER` | 답변 LLM 공급자: `fake`(기본, 시험용 가짜) / `gemini`(시험) / `openai`(운영, 값 `openai`). 설정이 잘못되면 서버는 뜨고 답변만 503 |
| `RAG_LLM_MODEL` | 공급자의 모델 이름. `gemini`·`openai` 는 꼭 넣는다 (기본값 없음). 운영 값은 `gpt-5.6-luna` (회의록 초안과 같음) |
| `RAG_ANSWER_PROMPT_VERSION` | 답변 지시문 버전 (`v1`, `v2` …). 없으면 `rag/prompts/answer/` 에서 번호가 가장 큰 파일. 없는 버전이면 답변만 503 |
| `RAG_LLM_TIMEOUT_S` | 답변 LLM 을 기다리는 상한 (기본 30초). 넘으면 `llm_timeout`. 재시도는 하지 않는다 |
| `RAG_LLM_FALLBACK_PROVIDER`, `RAG_LLM_FALLBACK_MODEL` | 폴백 모델. 기본 모델 호출이 실패하면(호출 오류 전부: 연결 실패, 시간 초과, 빈 응답, 429·5xx, 404·401 같은 설정 오류도. 실패마다 경고 로그가 남는다) 같은 입력으로 한 번 더 부른다. **공급자와 모델은 함께 넣는다** (요약 쪽 `LLM_FALLBACK_*` 와 달리 공급자를 생략할 수 없다). 둘 다 비면 폴백 없음. 한쪽만 있거나 틀리면 답변만 503. 서버에서만 쓰고 평가·손 시험 도구는 폴백하지 않는다. 답을 받은 뒤의 판정(`no_result`, 범위 밖 번호)에는 폴백하지 않는다. 폴백이 답하면 로그에 `answered_by=… fallback=1` |
| `RAG_CHAT_PROMPT_VERSION`, `RAG_REWRITE_PROMPT_VERSION` | 챗봇의 답하기·고쳐 쓰기 지시문 버전. 없으면 `rag/prompts/chat/`, `rag/prompts/rewrite/` 에서 번호가 가장 큰 파일. 없는 버전이면 챗봇만 503 |
| `RAG_REWRITE_TIMEOUT_S` | 챗봇 질문 고쳐 쓰기를 기다리는 상한 (기본 5초, 기본 모델만). 넘으면 원래 질문으로 이어 간다 |
| `RAG_CHAT_DEADLINE_S`, `RAG_CHAT_PRIMARY_TIMEOUT_S`, `RAG_CHAT_FALLBACK_MIN_S` | 챗봇 요청 하나의 마감 (기본 60초), 답하기의 기본 모델 상한 (기본 25초), 폴백을 부를 최소 남은 시간 (기본 10초). 명세 v0.2 4.4. 줄이는 쪽으로만 고친다 (백엔드 타임아웃 65초). 기본 모델 상한이 마감보다 크면 챗봇만 503 |
| `RAG_LLM_FALLBACK_COOLDOWN_S` | 기본 모델이 실패한 뒤 기본 모델을 건너뛰고 폴백만 부르는 시간 (기본 60초). 지나면 기본 모델을 다시 부른다. 폴백까지 가면 한 번의 답변이 시간 제한의 두 배(기본 약 60초)까지 걸릴 수 있다 |
| `GEMINI_API_KEY`(또는 `GOOGLE_API_KEY`), `OPENAI_API_KEY` | 공급자 API 키. 요약 서버와 같은 이름이라 하나로 같이 쓴다. 이미지에 넣지 않고 실행할 때 비밀값으로 |
| `RAG_DB_CONNECT_TIMEOUT_S` | 작업자가 DB 에 접속할 때 기다리는 상한 (기본 10초). 닿지 않는 주소에서 서버 종료가 늦어지지 않게 |
| `HF_HUB_CACHE` | 모델 캐시 위치. 없으면 `~/.cache/huggingface/hub`. 이미지에서는 `/models` 로 고정 (실행 사용자가 바뀌어도 같은 곳을 찾도록) |
