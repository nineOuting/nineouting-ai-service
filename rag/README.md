# rag

RAG 파이프라인 (문서 넣기·검색·답변). 작업 규칙은 `AGENTS.md`.

## 현재 상태

| 단계 | 상태 |
|---|---|
| 회의록 로더, 512토큰 나누기, 임베딩 | 구현 |
| 저장(pgvector, 버전 교체), 검색(workspaceId + docIds) | 구현 (함수까지, HTTP 없음) |
| 대기열·작업자, 콜백, API, PDF·워드·엑셀, 답변 | 예정 |

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
| `HF_HUB_CACHE` | 모델 캐시 위치. 없으면 `~/.cache/huggingface/hub`. 이미지에서는 `/models` 로 고정할 예정 (실행 사용자가 바뀌어도 같은 곳을 찾도록) |
