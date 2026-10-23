# 회의록 초안 API 명세 (AI-10·AI-11) v0.3 초안

> 회의록 초안을 만드는 API와, 확정 회의록을 검색 대상으로 넣을 때 바뀌는 RAG API를 다룬다. 백엔드(minutes-service)의 연동 형식 5.1(생성 요청)·5.2(결과 콜백)를 기준으로 하고, 원안에 없던 칸과 규칙은 **[ai-service 제안]**으로 표시한다. 확정되면 이 문서를 최신 전체본 하나로 교체한다.
> 요청 안의 요약 형식은 `meeting-summary-proposal.md`, 전사 줄(`seq`)의 정의는 `input-contract.md`를 따른다.

- 근거: PRD AI-10 회의록 초안, AI-11 근거·맥락 연결, LLM 공통 규칙(R1~R12), 백엔드 연동 형식 4장·5.1·5.2, RAG 명세
- 지라: SCRUM-95 (에픽), SCRUM-96 (이 문서), SCRUM-97 (초안 생성), SCRUM-98 (다시 배치), SCRUM-99 (근거·맥락 연결), SCRUM-100 (확정 회의록 검색 대상 넣기)

## 1. API 목록

| # | 장 | 방향 | 메서드 | 주소 | 용도 | 응답 | 상태 |
|---|---|---|---|---|---|---|---|
| 1 | [2장](#2-api-1--초안-생성-요청) | minutes-service → ai-service | `POST` | `/internal/minutes/drafts` | 초안 생성 요청 (양식 변경 시 다시 배치 포함) | `202` / `400` / `503` | **새 API.** 입력의 요약 형식은 요약 담당 합의 대기 |
| 2 | [3장](#3-api-2--초안-결과-콜백) | ai-service → minutes-service | `POST` | `{callbackUrl}` | 초안 결과 알림 | `2xx` | **새 API.** 줄 목록은 확정 가능, 글 모양은 화면 대기 |
| 3 | [4장](#4-api-34--rag-api-수정-scrum-100-ai-service-제안) | minutes-service → ai-service | `POST` | `/internal/rag/documents` | 확정 회의록을 검색 대상으로 넣기 | `202` / `400` / `409` / `503` | **기존 API 수정**: `minutes` 칸 형식만 바뀜 |
| 4 | [4장](#4-api-34--rag-api-수정-scrum-100-ai-service-제안) | minutes-service → ai-service | `POST` | `/internal/rag/answer` | 회의록 질문 답변 | `200` / `400` / `503` | **기존 API 수정**: 근거 위치 종류 하나 추가 |

- 장 순서는 API 번호와 같다: API 1은 2장, API 2는 3장, API 3·4는 4장. 그 뒤로 공통 객체(5장), 오류 코드(6장), 동작 규칙(7~9장)이 이어진다.
- 근거·맥락 연결(AI-11)은 ai-service API가 없다. 2번 결과의 줄마다 붙은 근거 정보를 백엔드가 따라가 보여 준다 (9장).
- 주소: 백엔드 원안은 1번을 `/drafts`로 적었다. ai-service 내부 API 주소 규칙(`/internal/...`)에 맞추자는 [ai-service 제안]이다.

### 공통 규칙

| 항목 | 규칙 |
|---|---|
| 형식 | `Content-Type: application/json`, UTF-8 |
| 필드 이름 | camelCase |
| 번호 | `minutesId`, `meetingId`, `agendaId`, `userId`는 `int` (양수). 요약 항목 `id`와 `requestId`는 `string` |
| 시각 (`datetime`) | ISO-8601 지역 시각 `string` (예: `2026-10-20T14:00:00`) |
| 근거 | 전사 줄 번호(`seq`) `int[]`. `seq`는 시간 순서가 아니므로 구간으로 쓰지 않는다 |
| 타입 표기 | `string`, `int`, `boolean`, `object`, 배열은 `T[]`, null이 올 수 있으면 `T \| null` |
| 오류 본문 | `{"error": "코드", "detail": "이유"}` (기존 API와 같음) |
| 요청 추적 | `X-Request-Id` 헤더 (선택). 받으면 로그에 남기고 결과 콜백에 그대로 붙인다 |

## 2. API 1 — 초안 생성 요청

```
POST /internal/minutes/drafts
Content-Type: application/json
X-Request-Id: (선택)
```

### 2.1 요청 본문

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `requestId` | `string` (UUID) | 예 | 요청 번호. 같은 번호로 다시 오면 다시 만들지 않는다 (2.3) |
| `minutesId` | `int` | 예 | 회의록 번호 |
| `meetingId` | `int` | 예 | 회의 번호 |
| `title` | `string` | 아니요 | 회의 이름. 초안 내용에는 쓰지 않는다 |
| `meetingStartAt` | `string` (datetime) | 아니요 | 회의 시작 시각. 초안 내용에는 쓰지 않는다 |
| `attendees` | `{userId: int, name: string}[]` | 아니요 | 참석자. 초안 내용에는 쓰지 않는다 |
| `template` | `object` | 예 | 회의록 양식 |
| `template.items` | `string[]` | 예 | 칸 이름 목록. 1~20개, 이름마다 1~100자, 서로 겹치지 않는다. 고정 양식이 아니며 사용자가 추가한 칸도 그대로 담는다. 결과는 이 순서·이름 그대로 돌려준다 |
| `agendas` | [`Agenda[]`](#51-agenda) | 예 | 안건 번호·제목·결정할 사항 |
| `summary` | [`MeetingSummary`](#52-meetingsummary) | 예 | 점검을 마친 회의 끝 요약. ai-service가 보낸 회의 끝 요약과 같은 형식에 사용자가 고친 내용을 반영한 것 [ai-service 제안, 요약 담당 합의 대기] |
| `previousLines` | [`PreviousLine[]`](#57-previousline) | 아니요 | 양식을 바꿔 다시 배치할 때 지금 초안의 줄 전부 (8장). 없거나 빈 목록이면 처음 만들기 [ai-service 제안] |
| `transcripts` | `array` | 아니요 | 백엔드 원안에 있으나 쓰지 않는다. 초안은 요약에서만 만든다. 보내면 무시한다 [ai-service 제안] |
| `callbackUrl` | `string` (URL) | 예 | 결과를 보낼 주소. 허용한 호스트만 받는다 (3.3) |

예시:

```json
{
  "requestId": "6f1c2a9e-1b7d-4c55-9d1a-3a8e0f4b2c10",
  "minutesId": 41,
  "meetingId": 31,
  "title": "주간 회의",
  "meetingStartAt": "2026-10-20T14:00:00",
  "attendees": [{"userId": 7, "name": "김철수"}, {"userId": 8, "name": "이영희"}],
  "template": {"items": ["안건별 논의", "결정 사항", "미결 안건", "할 일"]},
  "agendas": [
    {"agendaId": 101, "title": "배포 일정", "decisionPoint": "배포 날짜 확정"},
    {"agendaId": 102, "title": "예산", "decisionPoint": "광고 예산 승인"}
  ],
  "summary": {
    "agendas": [
      {
        "agendaId": 101,
        "status": "DECIDED",
        "statusReason": {"text": "1차 배포를 10월 30일(목)로 정하고 참석자가 동의함", "evidenceSeqs": [40, 42]},
        "summary": [
          {"id": "k3f9a1-1", "text": "1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴", "evidenceSeqs": [38, 39], "edited": false, "needsCheck": false}
        ],
        "opinions": [
          {"id": "k3f9a1-7", "text": "목요일은 QA 일정과 겹쳐 부담이 된다는 우려", "kind": "CONCERN", "evidenceSeqs": [41], "edited": false, "needsCheck": false}
        ],
        "todos": [
          {"id": "k3f9a1-10", "task": "QA 일정표 공유", "assigneeName": "김철수", "assigneeStatus": "MATCHED", "assigneeUserId": 7, "dueText": "수요일까지", "evidenceSeqs": [43], "edited": false, "needsCheck": false, "unclear": false}
        ]
      },
      {
        "agendaId": 102,
        "status": "UNDECIDED",
        "statusReason": {"text": "광고 예산 승인 여부를 정하지 못함", "evidenceSeqs": [88]},
        "summary": [
          {"id": "k3f9a1-3", "text": "광고 예산은 월 사용량을 확인한 뒤 정하기로 함", "evidenceSeqs": [86, 87], "edited": true, "needsCheck": false}
        ],
        "opinions": [],
        "todos": [
          {"id": "u-1", "task": "회의실 예약", "assigneeName": "이영희", "assigneeStatus": null, "assigneeUserId": null, "dueText": null, "evidenceSeqs": [], "edited": true, "needsCheck": false, "unclear": false}
        ]
      }
    ]
  },
  "previousLines": [],
  "callbackUrl": "http://minutes-service:8084/internal/minutes/41/draft-result"
}
```

### 2.2 응답

| 코드 | 경우 | 본문 |
|---|---|---|
| `202` | 접수함. 결과는 API 2로 알린다 | `{"requestId": "...", "status": "ACCEPTED"}` |
| `202` | 같은 `requestId`가 처리 중이거나 이미 끝남. 다시 만들지 않는다 | `{"requestId": "...", "status": "DUPLICATE"}` [ai-service 제안] |
| `400` | 형식 오류: 필수 칸 없음, 타입 틀림, 칸 이름 겹침, 상한 초과, 허용하지 않은 `callbackUrl` 등 | `{"error": "INVALID_INPUT", "detail": "이유"}`. 같은 요청은 다시 보내도 같은 결과 |
| `503` | 대기열이나 AI 설정을 쓸 수 없음 | `{"error": "NOT_READY", "detail": "이유"}`. 잠시 뒤 다시 요청 |

### 2.3 접수 규칙

| 항목 | 규칙 |
|---|---|
| 처리 | 접수한 요청은 대기열에서 하나씩 처리하고, 끝나면 API 2로 알린다 |
| 중복 | `requestId`를 7일 동안 기억한다 (저장소는 11장 ai-service 항목) |
| 보관 | 요청 내용은 처리가 끝나면 지운다. `requestId`, 상태, 오류 코드만 남긴다 |
| 로그 | 요약, 칸 이름, 초안 글을 남기지 않는다. `requestId`, `minutesId`, 개수, 상태, 오류 코드, 걸린 시간만 남긴다 |

## 3. API 2 — 초안 결과 콜백

```
POST {callbackUrl}
Content-Type: application/json
X-Request-Id: (요청에서 받았으면 그대로)
```

### 3.1 본문 — 성공

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `requestId` | `string` | 예 | 요청의 값 그대로 |
| `status` | `string` | 예 | `"SUCCEEDED"` |
| `sections` | [`Section[]`](#54-section) | 예 | `template.items`의 순서·이름 그대로 |
| `unplaced` | [`Source[]`](#56-source) | 예 | 어느 칸에도 넣지 못한 요약 항목. 없으면 빈 목록 [ai-service 제안] |
| `unplacedLines` | [`PreviousLine[]`](#57-previousline) | 예 | 다시 배치 때 새 양식에 맞는 칸이 없는 기존 줄. 처음 만들기에서는 항상 빈 목록 [ai-service 제안] |

예시:

```json
{
  "requestId": "6f1c2a9e-1b7d-4c55-9d1a-3a8e0f4b2c10",
  "status": "SUCCEEDED",
  "sections": [
    {
      "title": "안건별 논의",
      "content": "- 배포 일정: 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴\n- 배포 일정: 목요일은 QA 일정과 겹쳐 부담이 된다는 우려\n- 예산: 광고 예산은 월 사용량을 확인한 뒤 정하기로 함",
      "evidence": [
        {"sentence": "배포 일정: 1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴", "transcriptSeqs": [38, 39], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "summary", "id": "k3f9a1-1"}]},
        {"sentence": "배포 일정: 목요일은 QA 일정과 겹쳐 부담이 된다는 우려", "transcriptSeqs": [41], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "opinion", "id": "k3f9a1-7"}]},
        {"sentence": "예산: 광고 예산은 월 사용량을 확인한 뒤 정하기로 함", "transcriptSeqs": [86, 87], "edited": true, "needsCheck": false, "sources": [{"agendaId": 102, "kind": "summary", "id": "k3f9a1-3"}]}
      ]
    },
    {
      "title": "결정 사항",
      "content": "- 배포 일정: 1차 배포를 10월 30일(목)로 정했다.",
      "evidence": [
        {"sentence": "배포 일정: 1차 배포를 10월 30일(목)로 정했다.", "transcriptSeqs": [40, 42], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "decision", "id": null}]}
      ]
    },
    {
      "title": "미결 안건",
      "content": "- 예산: 광고 예산 승인",
      "evidence": [
        {"sentence": "예산: 광고 예산 승인", "transcriptSeqs": [88], "edited": false, "needsCheck": false, "sources": [{"agendaId": 102, "kind": "undecided", "id": null}]}
      ]
    },
    {
      "title": "할 일",
      "content": "- 배포 일정: QA 일정표 공유 (김철수, 수요일까지)\n- 예산: 회의실 예약 (이영희, 기한 미정)",
      "evidence": [
        {"sentence": "배포 일정: QA 일정표 공유 (김철수, 수요일까지)", "transcriptSeqs": [43], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "todo", "id": "k3f9a1-10"}]},
        {"sentence": "예산: 회의실 예약 (이영희, 기한 미정)", "transcriptSeqs": [], "edited": true, "needsCheck": false, "sources": [{"agendaId": 102, "kind": "todo", "id": "u-1"}]}
      ]
    }
  ],
  "unplaced": [],
  "unplacedLines": []
}
```

### 3.2 본문 — 실패

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `requestId` | `string` | 예 | 요청의 값 그대로 |
| `status` | `string` | 예 | `"FAILED"` |
| `error.code` | `string` | 예 | 실패 코드 (6장) |
| `error.message` | `string` | 예 | 사람이 읽는 설명. 요약·초안 내용은 담지 않는다 |

```json
{
  "requestId": "6f1c2a9e-1b7d-4c55-9d1a-3a8e0f4b2c10",
  "status": "FAILED",
  "error": {"code": "LLM_TIMEOUT", "message": "AI 응답 시간 초과"}
}
```

### 3.3 백엔드의 응답과 재전송

| 항목 | 규칙 |
|---|---|
| 백엔드 응답 | `2xx`면 전달 완료. 본문은 읽지 않는다. 백엔드는 `requestId`로 중복을 거른다 |
| 재전송 | `408`·`429`·`5xx`·연결 오류만 다시 보낸다. 3회, 간격 1초·2초·4초 (RAG 넣기 콜백과 같음). 그 밖의 `4xx`는 다시 보내지 않는다 |
| 보낼 곳 | 허용한 호스트(예: `minutes-service:8084`)로만 보낸다. 리다이렉트는 따라가지 않는다 |

## 4. API 3·4 — RAG API 수정 (SCRUM-100) [ai-service 제안]

RAG 명세의 API를 그대로 쓰고, 아래만 **덧붙인다.** 기존 칸과 동작은 없애지 않으므로 지금 쓰는 곳은 그대로 동작한다 (4.3). 새 API는 없다.

| API | 덧붙일 것 | 지금 (RAG 명세) | 이유 | 필요 |
|---|---|---|---|---|
| 3. `POST /internal/rag/documents` (`sourceType=minutes`) | `minutes` 칸에 확정 회의록 형식(4.1)도 받는다 | 회의 중 요약 결과(임시 형식 `schema_version` 0.1)만 받음 | RAG 명세를 만들 때 확정 회의록 형식이 없어 임시 형식을 두었다. 챗봇은 참석자가 검토·확정한 회의록을 찾아야 하고, 확정 회의록은 칸(이름 + 글) 모양이라 임시 형식으로 넣을 수 없다 | **필요.** 없으면 확정 회의록을 검색할 수 없다 |
| 4. `POST /internal/rag/answer` 응답 | `citations[].location`에 칸 이름 종류(4.2)를 더한다 | 녹음 시간(`type: time`)만 | 확정 회의록의 칸에는 녹음 시간이 없어 지금 방식으로는 위치를 보여 줄 수 없다. 줄마다 근거 시간을 담아 보내는 방법도 있으나, 백엔드의 줄 단위 저장이 필요하고 고친 줄은 근거가 맞지 않을 수 있다 (줄 단위 저장이 생기면 다시 검토) | **필요.** 없으면 확정 회의록 답변에 근거 위치를 보여 줄 수 없다 |
| 3. 같은 API의 버전 규칙 | 회의록은 정수 버전을 비교해, 저장된 것보다 낮은 버전은 무시 | 마지막에 받은 버전이 현재 버전 (순서 비교 없음) | 확정 뒤에도 고칠 수 있어(REQ-109) 같은 회의록이 여러 번 들어온다. 재시도로 옛 버전이 늦게 도착하면 옛 내용으로 덮어쓴다 | 선택. 백엔드가 순서를 지켜 보내면 빼도 된다 |

응답 코드와 나머지 칸은 RAG 명세 그대로다 (넣기 `202` `{docId, docVersion, status: "accepted"}` 후 완료 콜백, 답변 `200` `{status, answer, citations, errorCode}`).

### 4.1 넣기 요청의 `minutes` 칸

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `formatVersion` | `string` | 예 | `"1.0"` |
| `meetingId` | `int` | 예 | 회의 번호 |
| `meetingStartAt` | `string` (datetime) | 예 | 회의 시작 시각 |
| `confirmedAt` | `string` (datetime) | 아니요 | 확정 시각 |
| `sections` | `{title: string, content: string}[]` | 예 | 확정된 회의록의 칸 그대로. 근거 전사 줄은 담지 않는다 |

```json
{
  "docId": 41,
  "docVersion": "5",
  "workspaceId": 3,
  "sourceType": "minutes",
  "title": "주간 회의",
  "minutes": {
    "formatVersion": "1.0",
    "meetingId": 31,
    "meetingStartAt": "2026-10-20T14:00:00",
    "confirmedAt": "2026-10-21T09:30:00",
    "sections": [
      {"title": "결정 사항", "content": "- 배포 일정: 1차 배포를 10월 30일(목)로 정했다."},
      {"title": "할 일", "content": "- 배포 일정: QA 일정표 공유 (김철수, 수요일까지)\n- 예산: 회의실 예약 (이영희, 기한 미정)"}
    ]
  },
  "callbackUrl": "http://minutes-service:8084/internal/rag/ingest-result"
}
```

- `docId`는 회의록 번호(`minutesId`), `docVersion`은 확정 버전 번호를 문자열로 보낸다
- `content`의 줄 하나가 검색 단위 하나(청크)가 되고, 칸 이름을 앞에 붙여 검색에 쓴다. 사용자가 추가한 칸도 같은 방식으로 읽는다. "해당 내용 없음" 칸은 넣지 않는다
- 폴더를 옮길 때는 다시 보내지 않는다. 질문할 때 백엔드가 볼 수 있는 회의록 번호(`docIds`)를 넘기므로 폴더 소속은 백엔드만 알면 된다

### 4.2 답변 응답의 근거 위치 — 칸 이름 종류

| 칸 | 타입 | 설명 |
|---|---|---|
| `type` | `string` | `"section"` |
| `section` | `string` | 칸 이름 |
| `label` | `string` | 화면에 보여 줄 글 (칸 이름과 같음) |

```json
"location": {"type": "section", "section": "결정 사항", "label": "결정 사항"}
```

### 4.3 호환 규칙

지금 RAG API를 부르는 곳은 infra 통합 시험(`nineouting-infra/integration/e2e.py` 6단계)과 ai-service의 시험·손 시험 도구·데모 파일이다. 백엔드는 아직 부르지 않는다 (2026-10-23 확인). 모두 임시 형식을 보낸다.

| 변경 | 규칙 | 기존 호출에 미치는 영향 |
|---|---|---|
| `minutes` 형식 | **전환 기간에는 두 형식을 모두 받는다.** `minutes.formatVersion`이 `"1.0"`이면 확정 회의록 형식, `minutes.schema_version`이 `"0.1"`이면 임시 형식으로 읽는다. 둘 다 아니면 `400` | 없음 |
| 임시 형식 없애기 | 백엔드가 확정 회의록 형식으로 부르기 시작한 뒤, infra 통합 시험과 ai-service 시험·데모를 새 형식으로 바꾸고 나서 없앤다. 없애는 시점은 CN팀과 맞춘다 | 없앨 때 위 호출을 같이 바꾼다 |
| 이미 넣은 회의록 | 다시 넣지 않아도 그대로 검색된다. 근거 위치는 예전처럼 `type: time`으로 나온다 | 없음 |
| 근거 위치 종류 | 한 답변 안에 `type: time`(임시 형식으로 넣은 회의록)과 `type: section`(확정 회의록 형식)이 섞여 나올 수 있다. 화면은 `label`을 보여 주면 두 종류를 같은 방식으로 처리할 수 있다 | 없음 (통합 시험은 이 칸을 보지 않음) |
| 정수 버전 비교 | 저장된 버전과 새 버전이 **모두 정수 모양 문자열**(예: `"5"`)일 때만 비교한다. 하나라도 아니면 지금처럼 마지막에 받은 것이 현재 버전이다. 업로드 문서(`sourceType=upload`, 파일 해시)는 대상이 아니다 | 없음 (통합 시험은 `"1"`) |
| 낮은 버전 요청 | 처리하지 않고 바로 `202` + `{docId, docVersion, status: "ignored"}`로 답한다. 완료 콜백은 보내지 않는다 [ai-service 제안] | 해당 없음 (지금은 낮은 버전을 보내는 곳이 없음) |

## 5. 공통 객체

### 5.1 Agenda

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `agendaId` | `int` | 예 | 안건 번호 |
| `title` | `string` | 예 | 안건 제목. 줄 앞에 붙인다 (7.2) |
| `decisionPoint` | `string \| null` | 아니요 | 결정할 사항. 미결·결정 확인 필요 줄의 글이 된다. 없으면 그 자리를 비운다 (7.2) |

백엔드 원안의 `status`는 `summary` 안에 있으므로 보내도 쓰지 않는다.

### 5.2 MeetingSummary

[요약 담당 합의 대기] 자세한 정의와 이유는 `meeting-summary-proposal.md`에 있다.

회의 종료 뒤 마지막 안건 보드 본문 그대로다. 백엔드는 칸을 더하거나 빼지 않고, 점검에서 고친 내용만 반영한다.

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `agendas` | [`SummaryAgenda[]`](#53-summaryagenda와-요약-항목) | 예 | 안건 순서 그대로. 초안은 이 칸만 읽는다 |
| `version`, `basedOnSeq`, `currentAgendaId`, `offTopic`, `leftover` | 보드 문서 그대로 | 아니요 | 읽지 않는다. 있어도 되고 없어도 된다 |

### 5.3 SummaryAgenda와 요약 항목

`SummaryAgenda`:

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `agendaId` | `int` | 예 | 안건 번호 |
| `status` | `string` | 예 | `NOT_STARTED`, `DECIDED`, `UNDECIDED`, `DECISION_CHECK` (`agenda-status-contract.md`). 회의 종료 뒤에는 `DISCUSSING`이 나오지 않는다 (받아도 상태 줄을 만들지 않는다) |
| `statusReason` | `object \| null` | 예 | `{text: string, evidenceSeqs: int[]}`. `NOT_STARTED`이면 `null`. `DECIDED`이면 결정 줄의 재료 (7.1) |
| `summary` | `SummaryItem[]` | 예 | 안건 보드 요약 문장. 없으면 빈 목록 |
| `opinions` | `Opinion[]` | 예 | 안건 보드 의견. 없으면 빈 목록 |
| `todos` | `Todo[]` | 예 | 안건 보드 할 일. 없으면 빈 목록 |

요약 항목 공통 칸 (`SummaryItem`, `Opinion`, `Todo` 모두):

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | `string` | 예 | 회의 안에서 겹치지 않는 항목 번호. 사람이 점검에서 넣은 항목은 백엔드가 붙인다 (예: `u-1`) |
| `evidenceSeqs` | `int[]` | 예 | 근거 전사 줄. 빈 목록이면 사람이 점검에서 새로 넣은 항목 |
| `edited` | `boolean` | 예 | 사람이 점검에서 고친 항목이면 `true` |
| `needsCheck` | `boolean` | 예 | 고친 항목의 근거가 달라져 확인이 필요하면 `true` |

종류별 칸:

| 객체 | 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| `SummaryItem` | `text` | `string` | 예 | 요약 문장 |
| `Opinion` | `text` | `string` | 예 | 의견 한 줄 |
| | `kind` | `string` | 예 | `OPINION`(의견), `EVIDENCE`(근거), `CONCERN`(우려), `QUESTION`(질문) |
| `Todo` | `task` | `string` | 예 | 할 일 |
| | `assigneeName` | `string \| null` | 예 | 담당자 이름 (말한 그대로) |
| | `assigneeStatus` | `string \| null` | 예 | `MATCHED`, `UNASSIGNED`, `NOT_IN_ROSTER`, `AMBIGUOUS` (`agenda-opinions-contract.md` 4.2) |
| | `assigneeUserId` | `int \| string \| null` | 예 | 명단과 맞은 사용자 번호 |
| | `dueText` | `string \| null` | 예 | 기한 (말한 그대로, 날짜로 바꾸지 않음) |
| | `unclear` | `boolean` | 예 | 할 일인지 분명하지 않으면 `true`. 줄에 "할 일 확인 필요"를 붙인다 (7.2) |

### 5.4 Section

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `title` | `string` | 예 | 칸 이름 (`template.items`의 값) |
| `content` | `string` | 예 | 줄 목록의 글을 `- `를 붙여 줄바꿈으로 이은 일반 텍스트. 줄이 없으면 `"해당 내용 없음"` [화면 대기: 7.4] |
| `evidence` | [`Line[]`](#55-line) | 예 | **이 칸의 줄 목록.** 줄 하나에 하나. 백엔드 원안의 이름을 그대로 쓴다. 줄이 없으면 빈 목록 |

### 5.5 Line

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `sentence` | `string` | 예 | 줄의 글 (7.2). `content`에 `- ` 뒤로 그대로 들어 있다 |
| `transcriptSeqs` | `int[]` | 예 | 근거 전사 줄. 요약 항목의 근거를 코드가 옮긴다. 빈 목록이면 사람이 직접 넣은 항목 |
| `edited` | `boolean` | 예 | 요약 항목의 값 그대로 [ai-service 제안] |
| `needsCheck` | `boolean` | 예 | 요약 항목의 값 그대로 [ai-service 제안] |
| `sources` | [`Source[]`](#56-source) | 예 | 이 줄의 재료. 지금은 항상 1개 [ai-service 제안] |
| `draftEdited` | `boolean` | 아니요 | 다시 배치 결과에만 있다. 작성자가 초안에서 고쳤거나 새로 쓴 줄이면 `true` (8장) |

### 5.6 Source

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `agendaId` | `int` | 예 | 안건 번호 |
| `kind` | `string` | 예 | `decision`, `undecided`, `decisionCheck`, `summary`, `opinion`, `todo` (7.1) |
| `id` | `string \| null` | 예 | 요약 항목 번호. 안건 상태에서 나온 줄(`decision`, `undecided`, `decisionCheck`)은 `null` |

### 5.7 PreviousLine

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `title` | `string` | 예 | 그 줄이 지금 들어 있는 칸 이름 |
| `sentence` | `string` | 예 | 줄의 글. 작성자가 고쳤으면 고친 글 |
| `transcriptSeqs`, `edited`, `needsCheck`, `sources` | `Line`과 같음 | 예 | 결과 콜백에서 받아 저장한 값 그대로. 작성자가 새로 쓴 줄은 `transcriptSeqs`·`sources`가 빈 목록 |
| `draftEdited` | `boolean` | 예 | 작성자가 **초안에서** 고쳤거나 새로 쓴 줄이면 `true`. 요약 점검에서 고친 표시인 `edited`와 다르다 |

## 6. 오류 코드

| 코드 | 어디로 | 경우 |
|---|---|---|
| `INVALID_INPUT` | API 1 응답 `400` | 요청 형식 오류 |
| `NOT_READY` | API 1 응답 `503` | 대기열이나 AI 설정을 쓸 수 없음 |
| `INPUT_TOO_LONG` | API 2 실패 | 요약 항목이 상한을 넘음. 백엔드 원안의 `TRANSCRIPT_TOO_LONG` 대신 [ai-service 제안] |
| `LLM_TIMEOUT` | API 2 실패 | AI가 정해진 시간 안에 답하지 않음 (한 번 더 불러도 실패한 경우, 7.5) |
| `LLM_FAILED` | API 2 실패 | AI 호출 오류 (한 번 더 불러도 실패한 경우, 7.5) |
| `CHECK_FAILED` | API 2 실패 | 다시 시켜도 7.3의 검사를 통과하지 못함 |

## 7. 동작 규칙 — 초안 만들기 (API 1·2)

**초안의 내용은 점검을 마친 요약(`summary`)에서만 만든다.** 요약에 없는 내용은 만들지 않고, 맞는 내용이 없는 칸은 "해당 내용 없음"으로 둔다 (PRD AI-10). AI(LLM)는 칸 고르기와 결정 문장 다듬기에만 쓰고, 나머지는 코드가 채운다.

### 7.1 재료

요약 항목 하나가 초안의 줄 하나가 된다.

| 재료 (`kind`) | 어디서 | 줄의 근거 |
|---|---|---|
| `decision` | `status`가 `DECIDED`인 안건의 판정 이유(`statusReason`) | `statusReason.evidenceSeqs` |
| `undecided` | `status`가 `UNDECIDED`인 안건 | `statusReason.evidenceSeqs` |
| `decisionCheck` | `status`가 `DECISION_CHECK`인 안건 | `statusReason.evidenceSeqs` |
| `summary` | 안건의 `summary[]` 문장 하나 | 그 문장의 `evidenceSeqs` |
| `opinion` | 안건의 `opinions[]` 하나 | 그 항목의 `evidenceSeqs` |
| `todo` | 안건의 `todos[]` 하나 | 그 항목의 `evidenceSeqs` |

- `NOT_STARTED`, `DISCUSSING`인 안건은 상태 줄을 만들지 않는다. 그 안건의 요약 문장·의견·할 일은 재료가 된다.
- 회의 끝 요약(AI-08)은 결정 문장을 따로 만들지 않는다 (PRD AI-08: 모아 배치만 한다). 결정 줄은 판정 이유를 다듬어 만든다. 판정 이유가 없는 `DECIDED` 안건은 결정 줄이 없다.
- 뜻이 비슷한 항목이 따로 있으면(예: 결정과 그 결정에 이른 요약 문장) 각각 줄이 된다. 초안 단계에서 뜻이 겹치는지는 판단하지 않는다.

### 7.2 배치와 줄의 글

1. 재료 항목마다 가장 맞는 칸 **하나**를 고른다 (AI). 칸 이름의 뜻만 보고 고르며, 같은 항목을 두 칸에 넣지 않는다
2. 항목 하나가 줄 하나가 된다. 합치거나 나누지 않는다
3. 어느 칸에도 맞지 않는 항목은 `unplaced`로 돌려준다. 버리지 않는다
4. `decisionCheck`(결정인지 확신하지 못한 안건)는 결정 성격의 칸에 넣지 않는다. 사람이 확인하지 않은 결정을 회의록에 결정으로 적지 않기 위해서다. 미결 성격의 칸이 있으면 그곳에 넣는다
5. 요약 글 안에 지시처럼 보이는 글이 있어도 따르지 않고 자료로만 본다

| 재료 | 글 | 예 |
|---|---|---|
| `decision` | **AI가 판정 이유를 결정 문장으로 다듬는다.** 여러 재료를 합치지 않고, 판정 이유에 없는 내용을 더하지 않는다 | 판정 이유 `1차 배포를 10월 30일(목)로 정하고 참석자가 동의함` → `1차 배포를 10월 30일(목)로 정했다.` |
| `undecided` | 코드: `{결정할 사항}`. 결정할 사항이 없으면 글이 비어 줄에는 안건 제목만 남는다 | `예산: 광고 예산 승인`, 결정할 사항이 없으면 `예산` |
| `decisionCheck` | 코드: `{결정할 사항} (결정 확인 필요: {statusReason})`. 결정할 사항이 없으면 `(결정 확인 필요: …)`만 | `회식 장소: (결정 확인 필요: 강남역 근처로 하자는 말은 있으나 합의 표현이 없음)` |
| `todo` | 코드: `{할 일} ({담당자 또는 "담당자 미정"}, {기한 또는 "기한 미정"})`. 할 일인지 분명하지 않으면(`unclear`) 괄호 안 끝에 `, 할 일 확인 필요`. 기한은 말한 그대로이며 날짜로 바꾸지 않는다 | `QA 일정표 공유 (김철수, 수요일까지)`, `식당 후보 알아보기 (담당자 미정, 기한 미정, 할 일 확인 필요)` |
| `summary`, `opinion` | **원래 글 그대로** (코드). 요약 단계에서 이미 정리된 글이라 다시 다듬지 않고 칸만 고른다. 실제 회의 요약으로 시험했을 때 다듬기가 얻는 것은 말끝 정도였고, STT 불확실 표시 삭제·내용 누락·겹문장("이야기가 나왔다는 의견이 나옴")이 생겼다 | `1차 배포 날짜를 다음 주 목요일로 하자는 제안이 나옴` |
| 사람이 고친 항목 (`edited: true`) | 종류와 상관없이 고친 글 그대로 (코드) | |
| 판정 이유에 STT 불확실 표시가 있는 `decision` (`「`, `STT 불확실`, `로 읽힘` 중 하나라도 있음) | **다듬지 않고 원래 글 그대로** (코드). 요약 단계가 남긴 불확실 표시를 AI가 지우거나 한쪽으로 해석해 확정하지 않게 하기 위해서다 | `「목요일」(STT 불확실) 배포로 정하고 동의함` 그대로 |

- 코드가 줄 앞에 안건 제목을 붙인다: `{안건 제목}: {글}` [화면 대기]
- 칸 안의 줄 순서: 안건 순서 → 같은 안건 안에서 `decision`·상태 → `summary` → `opinion` → `todo` → 같은 종류 안에서는 요약의 순서

### 7.3 코드 검사

AI의 결과를 검사하고, 걸리면 걸린 이유를 붙여 다시 시킨다. 정해진 횟수 안에 통과하지 못하면 `CHECK_FAILED`로 답한다.

| 검사 | 걸리는 경우 |
|---|---|
| 칸 | `template.items`에 없는 칸 이름을 씀 |
| 항목 | 요약에 없는 항목을 가리킴 |
| 한 번만 | 같은 항목이 두 번 이상 쓰임 |
| 빠짐 | 어느 칸에도, `unplaced`에도 없는 항목이 있음 |
| 숫자·날짜 | AI가 다듬은 글에 재료에 없는 숫자나 날짜가 있음 |
| 빈 줄 | 글이 비었거나 줄바꿈이 들어 있음 |

### 7.4 화면에 따라 바뀔 수 있는 것 [화면 대기]

| 항목 | 지금 정한 기본값 | 바뀔 수 있는 것 |
|---|---|---|
| `content` | 줄 목록을 `- ` 줄로 이은 일반 텍스트 | 줄 단위 화면이면 백엔드가 `evidence`만 써도 된다 |
| 할 일 줄 | `{할 일} ({담당자}, {기한})` 한 줄 | 할 일을 표로 보여 주면 모양이 바뀐다 |
| 줄 앞 안건 제목 | `{안건 제목}: ` | 칸 안에서 안건별 제목줄로 묶을 수 있다 |
| `unplaced`, `unplacedLines` | 결과에 담는다 | 보여 줄 자리가 없으면 백엔드가 쓰지 않으면 된다 |

`content`는 `evidence`에서 만든 편의용이다. 화면이 정해져도 줄 목록(`evidence`)은 그대로다.

### 7.5 상한 [ai-service 제안]

| 항목 | 상한 | 넘으면 |
|---|---|---|
| `template.items` 수 | 20 | `400` |
| 칸 이름 길이 | 100자 (minutes-service 칸 이름 길이와 같음) | `400` |
| 요약 항목 수 | 300 | `INPUT_TOO_LONG` |
| AI 응답 시간 | 한 번에 60초 | 다시 부름 |
| 호출 실패(시간 초과·오류) 뒤 다시 부르기 | 1번. 같은 입력으로 부른다. 접수 후 콜백으로 알리는 기능이라 화면에서 기다리는 RAG 답변(다시 부르지 않음)과 다르게 둔다 | `LLM_TIMEOUT` / `LLM_FAILED` |
| 검사 실패 후 다시 시키기 | 2번. 호출 실패 뒤 다시 부르기와 따로 센다 | `CHECK_FAILED` |

정해 둔 설계값이며 측정값이 아니다. 처리 시간은 아직 재지 않았고, 실제 회의로 재 본 뒤 조정한다.

## 8. 동작 규칙 — 양식 변경 시 다시 배치 (API 1의 `previousLines`) [ai-service 제안, 화면 대기]

PRD AI-10의 예외 "초안 생성 후 양식을 바꾸면 정리본을 기준으로 초안을 다시 만든다. 기존 초안에서 이미 수정한 문장은 유지한다"를 위한 규칙이다. 화면에서 초안을 만든 뒤에도 양식을 바꿀 수 있는지가 정해지면 확정한다.

### 8.1 왜 "다시 배치"인가

양식이 바뀌면 칸의 이름뿐 아니라 뜻이 바뀔 수 있다 (예: "결정 사항" → "확정 일정"과 "예산"으로 나눔). 이름이 같은 칸끼리 글을 옮기면 뜻이 바뀐 칸을 알아채지 못한다. 그래서 **기존 초안의 모든 줄을 새 양식의 칸 뜻에 맞게 다시 고른다.** 작성자가 고친 글은 그대로 두고 들어갈 칸만 새로 정한다.

참고: 지금 minutes-service(dev)는 양식을 고르면 기존 칸을 모두 지우고 새 양식대로 빈 칸을 만든다.

### 8.2 요청

API 1에 새 `requestId`, 새 `template.items`, **처음과 같은 `summary`**를 보내고, 지금 초안의 줄 전부를 `previousLines`에 담는다.

```json
"previousLines": [
  {"title": "결정 사항", "sentence": "배포 일정: 1차 배포를 10월 30일(목) 오후로 정했다.", "transcriptSeqs": [40, 42], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "decision", "id": null}], "draftEdited": true},
  {"title": "할 일", "sentence": "배포 일정: QA 일정표 공유 (김철수, 수요일까지)", "transcriptSeqs": [43], "edited": false, "needsCheck": false, "sources": [{"agendaId": 101, "kind": "todo", "id": "k3f9a1-10"}], "draftEdited": false},
  {"title": "고객 요청", "sentence": "고객사 로고는 행사 전날까지 받기", "transcriptSeqs": [], "edited": false, "needsCheck": false, "sources": [], "draftEdited": true}
]
```

### 8.3 처리

| 줄 | 글 | 칸 |
|---|---|---|
| 작성자가 고친 줄 (`draftEdited: true`, `sources` 있음) | **그대로** | 그 줄의 재료 항목을 보고 새 양식에서 다시 고름 |
| 작성자가 새로 쓴 줄 (`draftEdited: true`, `sources` 없음) | **그대로** | 줄의 글을 보고 새 양식에서 다시 고름 |
| 고치지 않은 줄 (`draftEdited: false`) | 7.2의 규칙으로 다시 만듦 (AI가 다듬는 줄은 문구가 달라질 수 있음) | 재료 항목을 보고 새 양식에서 다시 고름 |
| 어느 칸에도 맞지 않는 줄 | 그대로 | `unplacedLines`로 돌려줌 |
| `previousLines` 어디에도 없는 재료 항목 (처음에 넣지 못했거나 작성자가 지운 것) | — | 다시 넣지 않고 `unplaced`로 돌려줌. 작성자가 지운 것을 되살리지 않기 위해서다 |

- 줄의 근거, 표시, 출처와 고친 글은 AI가 쓰지 않고 코드가 그대로 옮긴다.
- 7.3의 검사에 더해, `previousLines`의 모든 줄이 한 칸이나 `unplacedLines` 중 정확히 한 곳에 있는지 검사한다.
- 결과의 줄에는 `draftEdited`가 더해진다 (들어온 값 그대로, 다시 만든 줄은 `false`).

### 8.4 백엔드가 줄 단위 저장을 하기 전까지

`previousLines`를 만들려면 백엔드가 초안 줄마다 근거·표시·출처·초안에서 고쳤는지를 저장해야 한다 (지금은 칸의 글만 저장). 그 전까지는 백엔드가 이름이 같은 칸의 글을 옮기고, 새 양식에 없는 칸의 글은 따로 보여 주는 방식으로 임시 처리하길 제안한다.

## 9. 동작 규칙 — 근거·맥락 연결 (AI-11)

ai-service API가 없다. API 2 결과의 줄(`Line`)에 붙은 정보를 백엔드가 따라가 보여 준다. 전사의 보관과 30일 뒤 삭제는 백엔드(meeting-service)가 한다. ai-service는 회의 중에만 전사를 메모리에 두고 저장하지 않는다.

| 화면 동작 | 쓰는 정보 |
|---|---|
| 근거 발언 원문, 발언 시간 | 줄의 `transcriptSeqs`로 저장된 전사 줄을 꺼낸다 |
| 앞뒤 대화 | 근거 줄의 앞뒤 줄을 `startMs` 순서로 꺼낸다 (`seq`는 시간 순서가 아님. 범위는 11장) |
| 근거 구간 강조 | `transcriptSeqs`에 든 줄만 강조한다 |
| "직접 작성한 내용" | `transcriptSeqs`가 비어 있으면 |
| "수정됨" | `edited`가 `true`이면 (요약 점검에서 고침) |
| "확인 필요" | `needsCheck`가 `true`이면 |
| "전사 수정됨" | `transcriptSeqs` 중 사람이 고친 전사 줄이 있으면. 백엔드가 자기 전사 수정 기록과 대조한다 (ai-service는 전사 수정 기록이 없음) |
| 30일 뒤 전사 삭제 | `sources`의 `kind`가 `decision`이면 저장된 요약에서 그 안건(`agendaId`)의 판정 이유(`statusReason.text`)를 결정의 맥락으로 보여 준다. 결정이 아닌 줄은 11장 |

## 10. 백엔드 원안과 다른 곳

| # | 원안 (4장·5.1·5.2) | 이 명세 | 이유 |
|---|---|---|---|
| 1 | 주소 `/drafts` | `/internal/minutes/drafts` | 내부 API 주소 규칙 |
| 2 | `transcripts` 필수, 관련 발언 고르기는 AI가 판단 | 쓰지 않음 | 초안은 점검을 마친 요약에서만 만든다 |
| 3 | `summary` = 4장 형식 (`decisions`, `undecidedAgendaIds`, `uncertain`, `todos`) | 안건별 형식 (`meeting-summary-proposal.md`) | 안건별 논의를 채울 재료, 항목 번호, 수정 표시 |
| 4 | `agendas[].status` | `summary` 안의 상태를 씀 | 같은 정보가 두 곳에 있지 않게 |
| 5 | 다시 만들기 입력 없음 | `previousLines` | 양식 변경 시 고친 줄 유지 |
| 6 | 접수 응답 `202`만 | `202` 본문(`ACCEPTED`/`DUPLICATE`), `400`, `503` | 잘못된 요청을 바로 알림 |
| 7 | 예시의 "안건별 논의"에 전사에서 뽑은 내용 | 요약 문장·의견으로 채움 | 요약에서만 만든다 |
| 8 | 예시에서 결정·미결·할 일의 `evidence`가 빔 | 모든 줄에 근거. 예외는 사람이 직접 넣은 항목뿐 | PRD: 모든 문장이 근거를 이어받음 |
| 9 | `content` 형식 규정 없음 | `- ` 줄 목록, `evidence`는 줄마다 하나 | 줄과 근거를 정확히 잇기 위해 |
| 10 | 빈 칸 처리 없음 | `"해당 내용 없음"`, `evidence: []` | PRD 문구 |
| 11 | `evidence[]` = `sentence`, `transcriptSeqs` | `edited`, `needsCheck`, `sources` 추가 | 표시 유지, 다시 배치, 30일 뒤 결정 맥락 |
| 12 | 넣지 못한 내용 알림 없음 | `unplaced`, `unplacedLines` | 내용이 조용히 사라지지 않게 |
| 13 | 오류 코드 `TRANSCRIPT_TOO_LONG` | `INPUT_TOO_LONG`, `LLM_TIMEOUT`, `LLM_FAILED`, `CHECK_FAILED` | 전사를 안 쓰고, 실패 이유를 나눔 |
| 14 | 콜백 재전송 "몇 번" | 3회, 1·2·4초, `408`·`429`·`5xx`만 | RAG 콜백과 같은 규칙 |

## 11. 확인할 것

**요약 담당**

| 항목 | 내용 |
|---|---|
| 요약 형식 | `meeting-summary-proposal.md`의 형식을 회의 끝 요약(AI-08) 응답과 API 1의 `summary`로 쓸지 |

**CN팀 (백엔드)**

| 항목 | 내용 |
|---|---|
| **초안 줄 저장** | API 2의 줄마다 근거·표시·출처·초안에서 고쳤는지를 저장할 곳. 지금 minutes-service의 칸은 이름과 글만 저장한다. 이것이 없으면 근거 보여 주기(9장)와 다시 배치(8장)를 할 수 없다 |
| 요약 돌려보내기 | 받은 회의 끝 요약을 저장했다가, 사용자가 점검에서 고친 내용을 반영해 같은 형식으로 `summary`에 담을 수 있는지 (`meeting-summary-proposal.md` 4장) |
| 쓰는 요약 값 | `summary`는 회의 종료 뒤 마지막 안건 보드(`leftover.final: true`, 가장 큰 `version`) 본문 그대로인지 (5.2) |
| 점검 수정의 저장 위치 | 점검은 회의 종료 뒤의 일이고 ai-service는 회의 상태를 메모리에만 두어 재시작 때 잃을 수 있으므로, 수정은 백엔드 복사본에 저장하는 쪽이 안전하다 (요약 담당과 함께 정함) |
| "전사 수정됨" 표시 | 백엔드의 전사 수정 기록으로 표시할 수 있는지 (9장) |
| 원안과 다른 곳 | 10장의 표 |
| 양식 변경 처리 | 지금은 양식을 고르면 칸 내용을 모두 지운다. 8장의 다시 배치로 바꿀 수 있는지, 그 전까지의 임시 처리(8.4) |
| 콜백 호스트 | 운영 환경에서 콜백을 받는 서버의 호스트와 포트 (3.3) |
| 회의록 버전 | 확정 회의록의 버전이 정수로 늘어나는지. 낮은 버전을 무시하고 `"ignored"`로 답하는 규칙(4.3)을 넣어도 되는지 |
| 임시 형식 없애는 시점 | 백엔드가 확정 회의록 형식으로 RAG를 부르기 시작하는 시점, infra 통합 시험(`e2e.py` 6단계)을 새 형식으로 바꿀 시점 (4.3) |
| 볼 수 있는 회의록 수 | 답변 요청의 `docIds`는 최대 1,000개. 한 사용자가 볼 수 있는 확정 회의록이 이보다 많을 수 있는지 |
| 원문 문서 | 코드 주석이 가리키는 `docs/services/minutes-service.md`의 위치. 이 명세는 전달받은 4장·5장·6장을 기준으로 썼다 |

**ai-service (저희)**

| 항목 | 내용 |
|---|---|
| `requestId` 기억 저장소 | 7일 기억하려면 저장소가 필요하다. RAG용 PostgreSQL에 표를 더하면 초안 기능도 RAG DB가 있어야 켜진다 |
| 상한 값 | 7.5의 값을 실제 회의로 재 본 뒤 조정 |
