# AI 서버 미결 안건 추출 형식 (AI-07) v0.1 초안

> 회의를 끝내기 전에 결론이 나지 않은 안건 목록과 지금까지의 논의 요약을 안건 보드의 `leftover` 칸에 담는 방식을 다룬다. 종료 버튼을 눌렀다는 알림과 이어서 논의하기를 골랐다는 알림을 받는다. 아직 cn 팀과 확인하기 전의 AI 쪽 제안이다. 확정되면 이 문서를 최신 전체본 하나로 교체한다.
> 안건 상태는 `agenda-status-contract.md`, 요약 문장은 `agenda-summary-contract.md`, 보드 전체 모양은 `agenda-summary-contract.md`에 있다.

- 근거: PRD v0.1 AI-07, 요구사항 REQ-082~085, REQ-049·050·064(폴더의 남은 안건), 연동 API 초안 4장(오늘 회의 요약의 `undecidedAgendaIds`, `uncertain`)
- 설계 결정: `ai07-design-decisions-v1.md` (프로젝트 문서)
- 예시 파일: `examples/api/agenda-board-leftover-put.json`, `end-request-post.json`, `end-request-cancel-post.json`
- 평가: 모델을 부르지 않아 모델 평가는 하지 않는다. 자체 시험과 실수 주입 시험으로 확인한다

## 1. 목적과 범위

| 구분 | 내용 |
|---|---|
| 목적 | 회의 종료를 누르기 전에 결론 없이 남은 안건을 놓치지 않게 한다. 다음 회의로 넘길 안건과 그동안의 논의 요약을 함께 준다 |
| 다루는 것 | 안건 보드 `leftover` 채우기, 종료 버튼 알림 받기(종료 요청, 이어서 논의하기), 종료 직전 마지막 판정(flush) |
| 이 문서 밖 | 넘길 안건을 고르는 화면, 폴더의 남은 안건 저장·조회·이월 표시(백엔드가 한다. AI 서버는 저장하지 않는다), 오늘 회의 요약(AI-08, `agenda-today-summary-contract.md`. 같은 시점에 보드의 `todaySummary`로 나간다) |

## 2. 흐름

1. 사용자가 회의 종료를 누르면 백엔드가 4장의 요청(`request`)을 보낸다.
2. AI 서버는 이미 받은 줄을 모두 처리한 뒤, 한 번 기다리는 규칙을 건너뛰고 줄 분류, 안건 상태 판정, 안건 요약을 돌린다(5장).
3. 그 결과로 안건 보드를 만들어 보내고, 보드의 `leftover`에 넘길 후보 목록을 담는다(3장).
4. 백엔드는 `leftover.agendas`가 비어 있으면 바로 오늘 회의 요약으로 가고(REQ-085), 아니면 안건마다 "다음 회의로 넘기기"와 "이어서 논의하기"를 고르게 한다(REQ-082).
5. 이어서 논의하기를 고르면 백엔드가 4장의 요청(`cancel`)을 보낸다. 다음 보드에서 `leftover`가 `null`로 돌아간다. 회의는 그대로 이어지고 안건 상태는 확정되지 않았다. 결정이 나온 뒤 다시 종료를 누르면 1번부터 다시 한다(REQ-084).
6. 이월할 안건을 고르고 회의를 마무리하면 백엔드가 회의 종료 알림(`end`)을 보낸다. 이때 안건 상태가 확정되고 `leftover`는 `final: true`인 마지막 값이 된다.

**`end` 알림은 회의가 정말 끝날 때만 보내 주기를 요청한다.** `end`를 받으면 AI 서버는 안건 상태를 확정하고 이후 요청을 `ended`로 무시하므로, 종료 버튼에는 `end`가 아니라 4장의 요청을 써야 이어서 논의하기가 가능하다.

## 3. 보드의 leftover 칸

`leftover`는 보드의 맨 위 칸이다(`agendas[]` 안이 아니다. `offTopic`과 같은 층이다). 항상 들어 있고, 요청이 없으면 `null`이다.

```json
"leftover": {
  "requestRev": 3,
  "final": false,
  "agendas": [
    {
      "agendaId": 102,
      "status": "UNDECIDED",
      "statusReason": { "text": "결정할 사항 2개 중 1개가 결정됨 (미결 1개)", "evidenceSeqs": [51] },
      "decisionPoints": [
        { "decisionPointId": 11, "status": "DECIDED", "decisionText": "...", "evidenceSeqs": [47], "edited": false, "needsCheck": false },
        { "decisionPointId": 12, "status": "UNDECIDED", "decisionText": "...", "evidenceSeqs": [48, 51], "edited": false, "needsCheck": false }
      ],
      "summary": [
        { "id": "k3f9a1-3", "text": "...", "evidenceSeqs": [47, 48], "edited": false, "needsCheck": false }
      ]
    },
    {
      "agendaId": 103,
      "status": "NOT_STARTED",
      "statusReason": null,
      "decisionPoints": [
        { "decisionPointId": 13, "status": "NOT_STARTED", "decisionText": "...", "evidenceSeqs": [], "edited": false, "needsCheck": false }
      ],
      "summary": []
    }
  ]
}
```

| 값 | 뜻 |
|---|---|
| `leftover: null` | 종료 버튼 요청이 없거나 이어서 논의하기로 취소되었고, 회의도 끝나지 않았다 |
| `agendas: []` | 남은 안건이 없다. `null`과 다르다. 바로 오늘 회의 요약으로 가면 된다 |
| `requestRev` | 마지막으로 반영한 종료 버튼 요청(`request`)의 `rev`. 요청 없이 `end`만 온 경우 `null` |
| `final` | 회의가 끝났다(`end`를 받았다). 이때의 목록이 마지막 값이다 |
| `agendas[]` | 다음 회의로 넘길 후보. 보드의 `agendas[]`와 같은 순서다 |
| `agendas[].status` | 아래 3.1의 상태 규칙을 따른 값 |
| `agendas[].statusReason` | 보드의 `statusReason`과 같은 모양(`{text, evidenceSeqs}`). `NOT_STARTED`는 `null` |
| `agendas[].summary` | 지금까지의 논의 요약. 같은 시점 보드의 요약 문장(AI-03)을 **복사한 것**이다 |
| `agendas[].decisionPoints` | 그 안건의 결정할 사항마다 상태, 결정 내용, 근거. 같은 시점 보드의 `decisionPoints`(AI-04)를 **복사한 것**이다. 안건의 모든 사항이 들어 있어서, 결정된 사항과 아직 남은 사항을 `status`로 나눌 수 있다 |

- 안건 제목과 결정할 사항의 문구는 담지 않는다. 백엔드가 이미 가지고 있다. 사항마다 상태, 결정 내용, 근거는 `decisionPoints`로 담는다.
- `summary`의 문장은 보드와 같은 모양이다(`id`, `text`, `evidenceSeqs`, `edited`, `needsCheck`). 사람이 고친 문장(`edited`), 근거가 달라져 확인이 필요한 문장(`needsCheck`)도 그대로 담긴다. 새로 요약하지 않고, 의견·할 일은 넣지 않는다. 요약이 없는 안건(`NOT_STARTED` 등)은 빈 배열이다.
- 목록은 요청을 받은 뒤에도 새 줄이 들어오면 바뀐다. 바뀔 때만 보드를 다시 보낸다(`agenda-summary-contract.md` 전송 규칙 그대로).
- 보드는 안건 목록을 받은 뒤에만 나간다. 안건 목록을 받지 못한 회의에서는 요청을 받아도 `leftover`가 나가지 않는다.

### 3.1 상태 규칙

목록은 안건 상태를 **종료를 확정한다고 가정한 값**으로 판단한다.

| 안건의 상태 | 목록에서 |
|---|---|
| `DECIDED` | 넣지 않는다 |
| `DECISION_CHECK` | 넣는다 (`DECISION_CHECK`) |
| `UNDECIDED` | 넣는다 (`UNDECIDED`) |
| `DISCUSSING` | 넣는다 (`UNDECIDED`로 바꿔서) |
| `NOT_STARTED` | 넣는다 (`NOT_STARTED`) |

- 요청 중에는 보드의 `agendas[].status`와 목록의 `status`가 다를 수 있다. 보드의 `agendas[].status`는 이어서 논의하기로 돌아갈 수 있어서 요청 중에도 확정하지 않는다(예: 보드는 `DISCUSSING`, 목록은 `UNDECIDED`).
- 회의가 끝나면(`final: true`) 상태가 확정되어 보드의 상태와 목록의 상태가 같다.
- 사람이 안건 상태를 직접 고치는 기능은 범위 밖이다(`agenda-status-contract.md` 11장).
- 안건 상태는 결정할 사항의 상태에서 정한다(`agenda-status-contract.md` 2.2). 종료를 확정한다고 가정하므로, 사항 중 하나라도 미결이거나 논의 전이면 그 안건은 `UNDECIDED`이고 목록에 들어간다. 모든 사항이 결정이거나 확인 필요이고 확인 필요가 1개 이상이면 `DECISION_CHECK`로 들어간다.

## 4. 종료 버튼 알림 받기 (백엔드 → ai-service)

```
POST /internal/meetings/{meetingId}/end-requests
```

### 4.1 본문

| 필드 | 타입 | 설명 |
|---|---|---|
| `meetingId` | 문자열 또는 정수 | 주소의 값과 같아야 한다 |
| `action` | 문자열 | `request`(종료 버튼을 누름) 또는 `cancel`(이어서 논의하기를 고름) |
| `rev` | 정수 | 회의 안에서 계속 커진다. 이미 받은 값 이하이면 `stale`로 무시한다. 시작값은 상관없다 |

### 4.2 처리

- `request`를 받으면 목록을 만들어 다음 보드로 보낸다(2장). 같은 요청을 다시 받아도(같은 `rev`) 무시한다.
- `cancel`을 받으면 `leftover`가 `null`인 보드를 다음에 보낸다. 지금 떠 있는 요청이 없으면 `not_active`로 답하고, 이때도 `rev`는 기록한다. 순서가 바뀌어 `request(5)`가 `cancel(6)`보다 늦게 도착해도 `rev`로 정리된다.
- 요청을 받으면 바로 응답하고 목록 계산은 뒤에서 한다. 응답은 "받았다"는 뜻이고 목록은 보드로 온다.
- 회의가 끝난 뒤(`end` 이후)의 요청은 받지 않는다.

### 4.3 응답

| 상황 | 응답 |
|---|---|
| 반영함 | `{"accepted": true, "result": "accepted", "applied": true}` |
| `rev`가 오래됨 | `{"accepted": true, "result": "stale", "applied": false}` |
| 떠 있는 요청이 없는데 `cancel` | `{"accepted": true, "result": "not_active", "applied": false}` |
| 끝난 회의 | `{"accepted": false, "result": "ignored", "reason": "ended"}` |
| 형식 오류 | 400 `INVALID_INPUT` |
| 서버 준비 안 됨 | 503 `NOT_READY` |

## 5. 종료 버튼 직전의 마지막 판정 (flush)

종료 버튼 직전에 나온 말은 아직 판정에 반영되지 않았을 수 있다. 줄 분류는 약 30초 간격으로 하고, 안건 상태와 요약은 새 줄이 1개이면 최대 60초 기다린다. 그대로 두면 방금 결정된 안건이 목록에 남아 사용자가 이어서 논의하기를 누르게 된다.

그래서 `request`를 받으면 앞서 받은 줄 묶음을 모두 처리한 뒤 **한 번** 기다리는 규칙을 건너뛰고 아래를 돌린다.

| 단계 | flush 때 |
|---|---|
| 줄 분류 (AI-02) | 아직 분류하지 않은 줄을 바로 분류한다 |
| 안건 상태 판정 (AI-04) | 새 줄이 붙은 안건을 바로 판정한다. 종료 확정은 하지 않는다 |
| 안건 요약 (AI-03) | 새 줄이 붙은 안건을 바로 요약한다 |
| 의견·할 일 (AI-05) | 건너뛰지 않는다. 이월 데이터에 넣지 않으므로 기다리는 규칙을 그대로 따른다 |

- 새로 모델을 부르는 기능은 없다. 기존 세 기능의 호출이 요청 한 번에 새 줄이 쌓인 안건마다 최대 한 번씩 늘 수 있고, 기존 호출 상한(`MAX_CLASSIFY_CALLS_PER_MEETING`, `MAX_AGENDA_STATUS_CALLS_PER_MEETING`, `MAX_AGENDA_SUMMARY_CALLS_PER_MEETING`)이 그대로 적용된다. 모델 호출은 기존 `ai.task` 값(`agenda_classification`, `agenda_status`, `agenda_summary`)으로 지표에 남는다.
- 요청마다 한 번만 한다. 이어서 논의하기 뒤 다시 종료를 누르면 다시 한다.
- 분류된 결과가 바뀌면 줄별 분류 결과(`agenda-classification`)도 평소처럼 바뀐 줄만 보낸다.
- **요청이 도착하기 전에 백엔드가 아직 보내지 않은 줄 묶음(10초 주기)은 반영되지 않는다.** 요청 전에 쌓아 둔 줄 묶음을 먼저 보내 주기를 요청한다.

## 6. 설정

새 환경변수는 없다.

## 7. 토큰 지표

이 기능은 모델을 부르지 않는다. 새 `ai.task` 값도 없다. flush로 늘어나는 호출은 5장의 기존 값으로 남는다.

## 8. 복구

- AI 서버가 재시작하면 `leftover` 상태는 메모리에만 있어 사라진다. 줄 묶음 복구(`input-contract.md` 8장)와 함께 **백엔드가 보관해 둔 마지막 종료 버튼 요청(`action`, `rev`)을 줄 묶음보다 먼저 4장의 요청으로 다시 보내 주기**를 요청한다. 순서가 바뀌어 도착해도 `rev`로 정리된다.
- 보내 주지 않으면 이미 종료 버튼을 눌렀던 회의에서 목록이 사라진 채 이어질 수 있다. 다시 종료 버튼을 누르면 만들어진다.
- 다시 보낸 `request`도 flush를 한 번 한다.

## 9. 이번 범위에서 하지 않는 것

| 하지 않는 것 | 이유 |
|---|---|
| 넘길 안건을 고르고 폴더의 남은 안건에 저장하는 일 | 폴더 데이터는 백엔드가 가진다(AI 서버는 저장하지 않는다) |
| 이월된 안건의 불러오기·"진행 중" 표시(REQ-049·050·064) | 백엔드와 화면 몫이다 |
| 논의 요약을 새로 쓰기 | AI-03 요약 문장을 그대로 쓴다(요약 위에 요약을 쌓지 않는다) |
| 목록에 의견·할 일 담기 | 기획 확인 뒤 다시 논의한다 |
| 사람이 안건 상태를 직접 고치기 | PRD·명세서에 없다(AI-04 결정) |
| 종료 버튼 요청 없이 `end`만 온 경우의 이어서 논의하기 | `end`는 최종이다 |

한계

- 요청이 도착한 시점에 백엔드가 아직 보내지 않은 줄(최대 약 10초)은 목록에 반영되지 않는다.
- 안건 상태 판정은 기본으로 낮은 확신도 줄을 입력에서 빼서(`show_uncertain` 기본 꺼짐), 확답이 그 줄에만 있으면 결정이 `DECISION_CHECK`로 남을 수 있다(`agenda-status-contract.md`). 이때 그 안건이 이월 후보에 들어간다. 설정을 켜면 `[불확실]` 표시를 달아 보여 주지만 효과가 확인되지 않아 기본은 꺼 두었다.
- 요약 문장의 품질은 안건별 요약(AI-03)을 따른다. 평가는 회의 1건뿐이고 판정 보류 상태다.
- 안건 상태를 읽다가 실패하면 그 보드의 `leftover`는 `null`로 나가고 다음 보드에서 다시 시도한다.
- 목록은 요청을 받은 시점부터 새 줄이 들어오면 바뀐다. 사용자가 목록을 보는 동안 새 줄이 들어오면 화면의 목록이 바뀔 수 있다.

## 10. 협의 상태

| 항목 | 상태 |
|---|---|
| 종료 버튼 알림 `POST .../end-requests` (`request`/`cancel`, `rev`) | AI 제안, cn 팀 확인 필요 |
| `end`는 회의가 정말 끝날 때만 보낸다(종료 버튼에는 쓰지 않는다) | AI 제안, cn 팀 확인 필요 (요청서 D-8을 대체) |
| 요청 전에 쌓아 둔 줄 묶음을 먼저 보내 준다 | AI 제안, cn 팀 확인 필요 |
| 재시작 복구 때 마지막 종료 버튼 요청을 다시 보낸다 | AI 제안, cn 팀 확인 필요 |
| 보드 `leftover`의 모양 (`requestRev`, `final`, `agendas[]`) | AI 제안, cn 팀 확인 필요 |
| 목록 `agendas[]`에 `summary`(AI-03 요약 문장 사본)를 함께 담는다. 이월 저장에 필요한 칸이 더 있는지 | cn 팀 확인 필요 |
| `leftover: null`과 `agendas: []`의 구분(요청 전 / 남은 안건 없음) | AI 제안, cn 팀 확인 필요 |
| 연동 API 초안 4장의 `undecidedAgendaIds`, `uncertain`과의 관계(이 칸이 그 역할을 한다. 초안 4장은 오늘 회의 요약(AI-08)에서 다시 정한다) | cn 팀 확인 필요 |
| `DECISION_CHECK`를 이월 대상에 넣는다 | 기획 확인 필요 |
| 이어서 논의하기 뒤 종료 때 목록을 다시 보여 주는 흐름(REQ-084 비고) | 기획 확인 필요 (화면 처리) |
| 이월할 때 의견·할 일도 필요한지 | 기획 확인 필요 (지금은 요약 문장만) |
| 목록의 안건마다 `decisionPoints`(보드 사본)를 담는다. 이월 저장에 사항별 결정 내용과 근거까지 필요한지 | AI 제안, cn 팀 확인 필요 (cn 팀이 말한 남은 안건 항목은 안건명, 결정할 사항, 이월된 회의, 이월 날짜뿐이다) |

## 11. 예시 파일

| 파일 | 내용 |
|---|---|
| `examples/api/agenda-board-leftover-put.json` | 안건 보드 한 번의 본문 (종료 버튼 요청을 받은 상태. 보드는 논의 중인 안건이 목록에서는 미결로 담겨 있다). `decisionPoints`가 없는 옛 모양이며 구현 때 3장의 새 모양으로 바꾼다 |
| `examples/api/end-request-post.json` | 종료 버튼 요청 (`request`) |
| `examples/api/end-request-cancel-post.json` | 이어서 논의하기 (`cancel`) |

예시의 내용은 형식을 보여 주기 위해 만든 것이며 실제 회의가 아니다. 안건 번호는 연동 API 초안의 예시처럼 숫자로 썼다.
