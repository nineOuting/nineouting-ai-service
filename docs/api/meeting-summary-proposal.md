# 회의 끝 요약(AI-08) 형식 제안 — 회의록 초안 쪽 요청 v0.2 초안

> 회의록 초안(AI-10)이 입력으로 받기 좋은 회의 끝 요약 형식을 제안한다. AI-08은 요약 담당의 일이며, 이 문서는 초안을 만드는 쪽의 요청이다. 합의되면 AI-08 형식 문서로 옮기고 이 문서는 지운다.
> 안건 보드 항목의 정의는 `agenda-summary-contract.md`(요약 문장), `agenda-opinions-contract.md`(의견·할 일), `agenda-status-contract.md`(상태), `agenda-leftover-contract.md`(미결 안건)를 따른다.

- 근거: PRD AI-08 오늘 회의 요약, AI-09 점검, AI-10 회의록 초안. 백엔드 연동 형식 4장(회의 끝 요약 응답)·5.1(초안 생성 요청의 `summary`)
- 관련 문서: `minutes-draft-contract.md` (회의록 초안 명세)
- v0.2 (2026-10-23): 요약 담당 답변 반영. 결정 문장(`decision`)을 AI-08에서 만들지 않고, 초안이 판정 이유(`statusReason`)로 결정 문장을 다듬는다

## 1. 왜 제안하는가

| 문제 | 내용 |
|---|---|
| 형식이 두 벌 | 회의 중에는 안건 보드 항목(요약 문장, 의견, 할 일, 상태)이 있고, 백엔드 4장은 이와 다른 모양(`decisions`, `undecidedAgendaIds`, `uncertain`, `todos`)이다 |
| 초안 재료가 부족 | 4장 모양에는 안건별 논의 내용이 없어서, 초안의 "안건별 논의" 같은 칸과 사용자가 추가한 칸이 대부분 "해당 내용 없음"이 된다 (초안은 요약에 있는 내용만 쓴다, PRD AI-10) |
| 항목을 가리킬 번호가 없음 | 4장 항목에는 `id`가 없어, 초안 줄이 어느 요약 항목에서 왔는지 순서로만 가리킬 수 있다 |
| 수정 표시가 없음 | 4장 항목에는 사람이 고친 표시가 없다. 안건 보드 항목에는 이미 `edited`, `needsCheck`가 있다 |

## 2. 제안 원칙

1. **안건 보드 항목을 그대로 모은다.** PRD AI-08의 "새로 요약하지 않고 AI-03~05 결과를 모아 배치만 한다"와 같다. **AI-08이 새로 만드는 글은 없다**
2. 결정 문장은 회의록 초안(AI-10)이 `DECIDED` 안건의 판정 이유(`statusReason`)를 재료로 다듬는다. 여러 재료를 합쳐 새 문장을 쓰지는 않는다
3. 모든 항목은 `id`, `evidenceSeqs`, `edited`, `needsCheck`를 가진다 (안건 보드와 같은 칸)
4. **ai-service가 보낸 형식 그대로 백엔드가 돌려보낸다.** 사용자가 점검(AI-09)에서 고친 내용만 반영한다. 회의록 초안 요청의 `summary`가 이것이다

## 3. 형식

```json
{
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
}
```

타입 표기는 `string`, `int`, `boolean`, `object`, 배열은 `T[]`, null이 올 수 있으면 `T \| null`이다.

### 3.1 바깥 칸

회의 끝 요약은 **회의 종료(`end`) 뒤 마지막 안건 보드 본문**(`leftover.final: true`, 가장 큰 `version`)이다. 요약 담당 답변(2026-10-23, "지금은 보드 PUT으로 도착")에 따른 것이며, AI-08이 구현되면 다시 확인한다. 백엔드는 이 본문을 그대로 저장했다가 그대로 돌려보낸다 (칸을 더하거나 빼지 않는다).

| 칸 | 타입 | 회의록 초안 |
|---|---|---|
| `agendas` | `SummaryAgenda[]` | **읽는다.** 회의의 안건 순서 그대로 |
| `version`, `basedOnSeq`, `currentAgendaId`, `offTopic`, `leftover` | 보드 문서 그대로 (`agenda-summary-contract.md` 4장) | 읽지 않는다. 있어도 되고 없어도 된다 |

회의 번호는 보드 본문에 없고(주소에 있음), 초안 요청 바깥의 `meetingId`를 쓴다.

### 3.2 SummaryAgenda

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `agendaId` | `int` | 예 | 안건 번호 |
| `status` | `string` | 예 | `NOT_STARTED`, `DECIDED`, `UNDECIDED`, `DECISION_CHECK`. 안건 보드 최종판 그대로 (`agenda-status-contract.md`). 회의 종료 뒤에는 `DISCUSSING`이 `UNDECIDED`로 바뀌어 나오지 않는다 |
| `statusReason` | `{text: string, evidenceSeqs: int[]} \| null` | 예 | 안건 보드 최종판 그대로. `NOT_STARTED`이면 `null`. `DECIDED`이면 회의록 초안의 결정 문장 재료가 된다 |
| `summary` | `SummaryItem[]` | 예 | 안건 보드 요약 문장 그대로 (`agenda-summary-contract.md`). 없으면 빈 목록 |
| `opinions` | `Opinion[]` | 예 | 안건 보드 의견 그대로 (`agenda-opinions-contract.md` 4.1). 없으면 빈 목록 |
| `todos` | `Todo[]` | 예 | 안건 보드 할 일 그대로 (`agenda-opinions-contract.md` 4.2). 없으면 빈 목록 |

### 3.3 요약 항목 (`SummaryItem`, `Opinion`, `Todo`)

공통 칸:

| 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | `string` | 예 | 항목 번호 (3.4) |
| `evidenceSeqs` | `int[]` | 예 | 근거 전사 줄 번호. ai-service가 만든 항목은 1개 이상. 빈 목록이면 사람이 점검에서 새로 넣은 항목 (4장) |
| `edited` | `boolean` | 예 | 사람이 고친 항목이면 `true`. ai-service가 보낼 때는 안건 보드 값 그대로 |
| `needsCheck` | `boolean` | 예 | 고친 항목의 근거가 달라져 확인이 필요하면 `true`. 안건 보드와 같은 뜻 |

종류별 칸:

| 객체 | 칸 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| `SummaryItem` | `text` | `string` | 예 | 요약 문장 |
| `Opinion` | `text` | `string` | 예 | 의견 한 줄 |
| | `kind` | `string` | 예 | `OPINION`(의견), `EVIDENCE`(근거), `CONCERN`(우려), `QUESTION`(질문) |
| `Todo` | `task` | `string` | 예 | 할 일 |
| | `assigneeName` | `string \| null` | 예 | 담당자 이름 (말한 그대로). 없으면 `null`이고 `assigneeStatus`는 `UNASSIGNED`. 회의록에는 "담당자 미정"으로 쓴다 |
| | `assigneeStatus` | `string \| null` | 예 | `MATCHED`, `UNASSIGNED`, `NOT_IN_ROSTER`, `AMBIGUOUS` |
| | `assigneeUserId` | `int \| string \| null` | 예 | 명단과 맞은 사용자 번호 |
| | `dueText` | `string \| null` | 예 | 기한. 말한 그대로이며 날짜로 바꾸지 않는다 (회의록 초안도 바꾸지 않는다). 없으면 `null`이고 회의록에는 "기한 미정"으로 쓴다 |
| | `unclear` | `boolean` | 예 | 할 일인지 분명하지 않으면 `true`. 회의록에는 "할 일 확인 필요"를 붙인다 |

### 3.4 항목 번호 `id`

`id`는 **요약 항목 하나하나의 번호**다. 회의록 초안은 초안의 줄마다 "어느 요약 항목에서 나왔는지"를 이 번호로 가리킨다 (양식 변경 시 다시 배치, 사람이 지운 항목을 되살리지 않기에 쓴다). 안건 상태에서 나온 줄(결정, 미결, 결정 확인 필요)은 요약 항목이 아니라 번호가 없고, 안건 번호로 가리킨다.

| 항목 | 무엇의 번호 | 누가 매기나 |
|---|---|---|
| `summary[].id` | 요약 문장 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| `opinions[].id` | 의견 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| `todos[].id` | 할 일 하나 | ai-service, 안건 보드를 만들 때 (이미 있음) |
| 사람이 점검에서 넣은 항목의 `id` | 그 항목 | 백엔드 (예: `u-1`) |

규칙 (안건 보드 `agenda-summary-contract.md`의 문장 번호 규칙과 같다. 요약 담당 확인 2026-10-23):

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

회의록 초안 쪽 요구는 하나다. **초안 요청의 `summary`에 점검 수정이 반영되어 있고, 항목 `id`·`edited`·`needsCheck`가 유지되어야 한다.** 수정을 어디에 저장할지는 6장에서 정한다.

## 5. 백엔드 4장과의 대응

| 백엔드 4장 | 이 제안 | 차이 |
|---|---|---|
| `decisions[]` (`agendaId`, `text`, `context`, `evidenceSeqs`) | `status`가 `DECIDED`인 안건의 `statusReason` | AI-08은 결정 문장을 만들지 않는다. 회의록 초안이 판정 이유로 결정 문장을 다듬는다 |
| `undecidedAgendaIds` | `status`가 `UNDECIDED`인 안건 | 미결 안건 추출(AI-07)의 이월 목록(`leftover`)과는 다르다. 이월 목록에는 미결 말고 "아직 안 다룸"과 "결정 확인 필요"도 들어간다 |
| `uncertain[]` (`agendaId`, `reason`, `evidenceSeqs`) | `status`가 `DECISION_CHECK`인 안건의 `statusReason` | 같은 정보 |
| `todos[]` (`task`, `assigneeName`, `dueDate`, `evidenceSeqs`) | `agendas[].todos` | 안건 안으로 들어감. 기한이 날짜(`dueDate`)가 아니라 말 그대로(`dueText`). `assigneeUserId` 등 추가 |
| 없음 | `agendas[].summary`, `agendas[].opinions` | 새로 담김. 초안의 "안건별 논의"와 사용자가 추가한 칸의 재료가 된다 |

## 6. 정할 것

| 항목 | 누구 | 내용 |
|---|---|---|
| 형식 채택 | 요약 담당, 백엔드 | 이 형식을 회의 끝 요약과 초안 요청의 `summary`로 쓸지. 요약 담당은 안건마다 요약·의견·할 일을 묶는 모양으로 바꾸기로 함 (2026-10-23) |
| 결정 판정 | 요약 담당 | 평가에서 AI-04가 `DECIDED`를 거의 내지 않았다. 그러면 회의록의 "결정 사항"이 대부분 빈다 |
| 점검 수정의 저장 위치 | 요약 담당, 백엔드 | 점검은 회의가 끝난 뒤의 일이다. ai-service는 회의 상태를 메모리에만 두어 재시작 때 잃을 수 있으므로, 백엔드 복사본에 저장하는 쪽이 안전하다 (회의록 초안 쪽 의견) |
| 쓰는 값 | 백엔드 | 초안 요청의 `summary`는 회의 종료 뒤 마지막 안건 보드(`leftover.final: true`, 가장 큰 `version`)에 점검 수정을 반영한 것인지. 보드에는 `final` 칸이 없고 `leftover` 안에 있다 |
| 점검 화면에서 할 수 있는 것 | 기획, 프론트 | 항목 고치기 말고 추가·삭제·결정 여부 바꾸기가 되는지 (4장 표). `DECISION_CHECK`를 사람이 결정이나 미결로 정할 수 있으면 회의록에 확인되지 않은 결정이 남지 않는다 |
