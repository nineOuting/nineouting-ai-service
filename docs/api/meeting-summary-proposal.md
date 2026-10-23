# 회의 끝 요약(AI-08) 형식 제안 — 회의록 초안 쪽 요청 v0.1 초안

> 회의록 초안(AI-10)이 입력으로 받기 좋은 회의 끝 요약 형식을 제안한다. AI-08은 요약 담당의 일이며, 이 문서는 초안을 만드는 쪽의 요청이다. 합의되면 AI-08 형식 문서로 옮기고 이 문서는 지운다.
> 안건 보드 항목의 정의는 `agenda-summary-contract.md`(요약 문장), `agenda-opinions-contract.md`(의견·할 일), `agenda-status-contract.md`(상태), `agenda-leftover-contract.md`(미결 안건)를 따른다.

- 근거: PRD AI-08 오늘 회의 요약, AI-09 점검, AI-10 회의록 초안. 백엔드 연동 형식 4장(회의 끝 요약 응답)·5.1(초안 생성 요청의 `summary`)
- 관련 문서: `minutes-draft-contract.md` (회의록 초안 명세)

## 1. 왜 제안하는가

| 문제 | 내용 |
|---|---|
| 형식이 두 벌 | 회의 중에는 안건 보드 항목(요약 문장, 의견, 할 일, 상태)이 있고, 백엔드 4장은 이와 다른 모양(`decisions`, `undecidedAgendaIds`, `uncertain`, `todos`)이다 |
| 초안 재료가 부족 | 4장 모양에는 안건별 논의 내용이 없어서, 초안의 "안건별 논의" 같은 칸과 사용자가 추가한 칸이 대부분 "해당 내용 없음"이 된다 (초안은 요약에 있는 내용만 쓴다, PRD AI-10) |
| 항목을 가리킬 번호가 없음 | 4장 항목에는 `id`가 없어, 초안 줄이 어느 요약 항목에서 왔는지 순서로만 가리킬 수 있다 |
| 수정 표시가 없음 | 4장 항목에는 사람이 고친 표시가 없다. 안건 보드 항목에는 이미 `edited`, `needsCheck`가 있다 |

## 2. 제안 원칙

1. **안건 보드 항목을 그대로 모은다.** 기존 문서의 "AI-08은 새로 요약하지 않고 이 요약을 모아 배치한다"(`agenda-summary-contract.md`)와 같다
2. **새로 만드는 것은 안건마다 결정 문장과 그 맥락(`decision`) 하나뿐이다**
3. 모든 항목은 `id`, `evidenceSeqs`, `edited`, `needsCheck`를 가진다 (안건 보드와 같은 칸)
4. **ai-service가 보낸 형식 그대로 백엔드가 돌려보낸다.** 사용자가 점검(AI-09)에서 고친 내용만 반영한다. 회의록 초안 요청의 `summary`가 이것이다

## 3. 형식

```json
{
  "meetingId": 31,
  "agendas": [
    {
      "agendaId": 101,
      "status": "DECIDED",
      "statusReason": {"text": "목요일 배포에 참석자가 동의함", "evidenceSeqs": [40, 42]},
      "decision": {"id": "d-101", "text": "10월 30일(목) 배포", "context": "QA가 수요일까지 가능하다고 확인", "evidenceSeqs": [40, 42], "edited": false, "needsCheck": false},
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
      "decision": null,
      "summary": [
        {"id": "k3f9a1-3", "text": "광고 예산은 월 사용량을 확인한 뒤 정하기로 함", "evidenceSeqs": [86, 87], "edited": true, "needsCheck": false}
      ],
      "opinions": [],
      "todos": [
        {"id": "u-1", "task": "회의실 예약", "assigneeName": "이영희", "assigneeStatus": null, "assigneeUserId": null, "dueText": null, "evidenceSeqs": [], "edited": true, "needsCheck": false, "unclear": false}
      ]
    }
  ]
}
```

타입 표기는 `string`, `int`, `boolean`, `object`, 배열은 `T[]`, null이 올 수 있으면 `T \| null`이다.

### 3.1 바깥 칸

| 칸 | 타입 | 필수 | 설명 | 새로 만드나 |
|---|---|---|---|---|
| `meetingId` | `int` | 예 | 회의 번호 | 아니요 |
| `agendas` | `SummaryAgenda[]` | 예 | 회의의 안건 순서 그대로 | 아니요 |

### 3.2 SummaryAgenda

| 칸 | 타입 | 필수 | 설명 | 새로 만드나 |
|---|---|---|---|---|
| `agendaId` | `int` | 예 | 안건 번호 | 아니요 |
| `status` | `string` | 예 | `NOT_STARTED`, `DISCUSSING`, `DECIDED`, `UNDECIDED`, `DECISION_CHECK`. 안건 보드 최종판 그대로 (`agenda-status-contract.md`) | 아니요 |
| `statusReason` | `{text: string, evidenceSeqs: int[]} \| null` | 예 | 안건 보드 최종판 그대로. `NOT_STARTED`이면 `null` | 아니요 |
| `decision` | `Decision \| null` | 예 | 결정 문장과 결정의 맥락. `status`가 `DECIDED`일 때만 있고, 아니면 `null` | **예 (AI-08이 새로 만드는 유일한 것)** |
| `summary` | `SummaryItem[]` | 예 | 안건 보드 요약 문장 그대로 (`agenda-summary-contract.md`). 없으면 빈 목록 | 아니요 |
| `opinions` | `Opinion[]` | 예 | 안건 보드 의견 그대로 (`agenda-opinions-contract.md` 4.1). 없으면 빈 목록 | 아니요 |
| `todos` | `Todo[]` | 예 | 안건 보드 할 일 그대로 (`agenda-opinions-contract.md` 4.2). 없으면 빈 목록 | 아니요 |

### 3.3 요약 항목 (`Decision`, `SummaryItem`, `Opinion`, `Todo`)

공통 칸:

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | `string` | 예 | 항목 번호 (3.4) |
| `evidenceSeqs` | `int[]` | 예 | 근거 전사 줄 번호. ai-service가 만든 항목은 1개 이상. 빈 목록이면 사람이 점검에서 새로 넣은 항목 (4장) |
| `edited` | `boolean` | 예 | 사람이 고친 항목이면 `true`. ai-service가 보낼 때는 안건 보드 값 그대로이고, `decision`은 `false` |
| `needsCheck` | `boolean` | 예 | 고친 항목의 근거가 달라져 확인이 필요하면 `true`. 안건 보드와 같은 뜻 |

종류별 칸:

| 객체 | 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| `Decision` | `text` | `string` | 예 | 결정 문장 |
| | `context` | `string \| null` | 예 | 결정의 맥락 (왜 그렇게 정했는지) |
| `SummaryItem` | `text` | `string` | 예 | 요약 문장 |
| `Opinion` | `text` | `string` | 예 | 의견 한 줄 |
| | `kind` | `string` | 예 | `OPINION`(의견), `EVIDENCE`(근거), `CONCERN`(우려), `QUESTION`(질문) |
| `Todo` | `task` | `string` | 예 | 할 일 |
| | `assigneeName` | `string \| null` | 예 | 담당자 이름 (말한 그대로) |
| | `assigneeStatus` | `string \| null` | 예 | `MATCHED`, `UNASSIGNED`, `NOT_IN_ROSTER`, `AMBIGUOUS` |
| | `assigneeUserId` | `int \| string \| null` | 예 | 명단과 맞은 사용자 번호 |
| | `dueText` | `string \| null` | 예 | 기한. 말한 그대로이며 날짜로 바꾸지 않는다 |
| | `unclear` | `boolean` | 예 | 할 일인지 분명하지 않으면 `true` |

### 3.4 항목 번호 `id`

`id`는 **요약 항목 하나하나의 번호**다. 회의록 초안은 초안의 줄마다 "어느 요약 항목에서 나왔는지"를 이 번호로 가리킨다 (양식 변경 시 다시 배치, 30일 뒤 결정 맥락 찾기, 사람이 지운 항목을 되살리지 않기에 쓴다).

| 항목 | 무엇의 번호 | 누가 매기나 |
|---|---|---|
| `summary[].id` | 요약 문장 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| `opinions[].id` | 의견 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| `todos[].id` | 할 일 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| `decision.id` | 결정 하나 | ai-service, AI-08이 회의 끝 요약을 만들 때 (새로) |
| 사람이 점검에서 넣은 항목의 `id` | 그 항목 | 백엔드 (예: `u-1`) |

규칙 (안건 보드 `agenda-summary-contract.md`의 문장 번호 규칙과 같다):

- 뜻이 없는 문자열이다. 순서나 종류를 번호에서 읽지 않는다
- 회의 안에서 겹치지 않는다. 서버가 재시작된 뒤에도 겹치지 않게 만든다
- 사람이 고친 항목은 같은 번호를 유지한다
- 백엔드가 붙이는 번호는 ai-service가 붙이는 번호와 겹치지 않게 접두어를 쓴다 (예: `u-`). ai-service는 이 접두어를 쓰지 않는다

## 4. 사람이 점검(AI-09)에서 고칠 때 (백엔드)

| 사용자 동작 | 돌려보낼 때 |
|---|---|
| 항목 글을 고침 | 그 항목의 `edited`를 `true`로 |
| 항목을 새로 넣음 | 백엔드가 `id`를 붙인다 (3.4, 예: `u-1`). `evidenceSeqs`는 빈 목록, `edited`는 `true`. 근거 줄이 없는 항목은 사람이 넣은 것뿐이다 |
| 항목을 지움 | 목록에서 뺀다 |
| 결정인지 아닌지 고침 | (정할 것, 6장) |

## 5. 백엔드 4장과의 대응

| 백엔드 4장 | 이 제안 | 차이 |
|---|---|---|
| `decisions[]` (`agendaId`, `text`, `context`, `evidenceSeqs`) | `agendas[].decision` | 안건 안으로 들어감. `id`, `edited`, `needsCheck` 추가 |
| `undecidedAgendaIds` | `status`가 `UNDECIDED`인 안건 | 미결 안건 추출(AI-07)의 `leftover`와 같은 판정 |
| `uncertain[]` (`agendaId`, `reason`, `evidenceSeqs`) | `status`가 `DECISION_CHECK`인 안건의 `statusReason` | 같은 정보 |
| `todos[]` (`task`, `assigneeName`, `dueDate`, `evidenceSeqs`) | `agendas[].todos` | 안건 안으로 들어감. 기한이 날짜(`dueDate`)가 아니라 말 그대로(`dueText`). `assigneeUserId` 등 추가 |
| 없음 | `agendas[].summary`, `agendas[].opinions` | 새로 담김. 초안의 "안건별 논의"와 사용자가 추가한 칸의 재료가 된다 |

## 6. 정할 것

| 항목 | 누구 | 내용 |
|---|---|---|
| 형식 채택 | 요약 담당, 백엔드 | 이 형식을 회의 끝 요약 응답과 초안 요청의 `summary`로 쓸지 |
| 기한 형식 | 기획 | 화면이나 회의록에 날짜(`dueDate`)가 꼭 필요한지. 필요하면 AI-08이 `dueText`와 `meetingStartAt`으로 날짜를 더할지 |
| 점검 화면에서 할 수 있는 것 | 기획, 프론트 | 항목 고치기 말고 추가·삭제·결정 여부 바꾸기가 되는지 (4장 표) |
| 응답 크기 | 요약 담당 | 요약 문장·의견까지 담으면 응답이 커진다. 백엔드 4장의 "동기 응답, 목표 10초"와 맞는지 |
