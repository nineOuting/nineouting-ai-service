# AI 서버 오늘 회의 요약 정리본 형식 (AI-08) v0.3 초안

> 회의를 끝내기 전에 모두가 같은 결론을 확인하도록, 회의 중 화면에 보였던 결과를 안건별로 묶어 안건 보드의 `todaySummary` 칸에 담는 방식을 다룬다. 새로 요약하지 않고 이미 있는 값을 복사해 담는다. 회의록 초안(AI-10)이 이 칸을 그대로 재료로 쓴다. 화면(와이어프레임)이 확정되기 전의 임시 모양이며, 아직 cn 팀과 확인하기 전의 AI 쪽 제안이다. 확정되면 이 문서를 최신 전체본 하나로 교체한다.
> 안건 상태는 `agenda-status-contract.md`, 요약 문장은 `agenda-summary-contract.md`, 의견·할 일은 `agenda-opinions-contract.md`, 미결 안건과 종료 버튼 알림은 `agenda-leftover-contract.md`에 있다.

- 근거: PRD v0.1 AI-08·AI-10, 요구사항 REQ-085~087, 연동 API 초안 4장(오늘 회의 요약), 회의록 초안 명세(요약 형식 제안)
- 설계 결정: `ai08-design-decisions-v1.md` (프로젝트 문서)
- 예시 파일: `examples/api/agenda-board-today-summary-put.json`
- 평가: 모델을 부르지 않아 모델 평가는 하지 않는다. 자체 시험, 실수 주입 시험, 확정 정답으로 만든 보드의 정합성 점검(`summary/eval_today_summary.py`)으로 확인한다
- v0.2에서 바뀐 것: 안건 한 칸에 결정할 사항마다 상태, 결정 내용, 근거(`decisionPoints[]`)를 더했다. 결정 문장은 이 사항별 `decisionText`가 대신한다 (3.2). cn 팀이 "결정 문장 없이 `statusReason`으로 대신하는 안"은 받기 어렵다고 답해서 바꿨다
- v0.1에서 바뀐 것: 결정/이월/할 일 세 덩어리를 없애고 안건별 묶음(`agendas[]`)으로 바꿨다. 담당자·기한 미정 표시 칸(`assigneeUndecided`, `dueUndecided`)을 뺐다. 결정 문장(`decision`)은 만들지 않는다 (3.2)

## 1. 목적과 범위

| 구분 | 내용 |
|---|---|
| 목적 | 회의실을 나가기 전에 참석자 모두가 같은 결론(결정된 사항, 그 맥락, 이월 안건, 할 일)을 한 화면에서 확인한다. 이 정리본이 회의록 초안의 재료가 된다 |
| 다루는 것 | 안건 보드 `todaySummary` 채우기 (안건마다 상태, 판정 근거, 결정할 사항별 결과, 요약 문장, 의견, 할 일) |
| 이 문서 밖 | 정리본 화면, 정리본의 저장과 1년 보관(백엔드), 사용자가 고른 이월 선택 결과(백엔드만 안다), 점검 화면에서 사람이 고친 내용(백엔드가 저장), 회의록 초안(AI-10) |

원칙: **새로 요약하지 않는다.** 보드에 이미 있는 값(AI-03 요약 문장, AI-04 상태·판정 근거·결정할 사항별 결과, AI-05 의견·할 일)을 복사해 안건별로 묶을 뿐이다. 근거 줄 번호(`evidenceSeqs`)도 원래 값 그대로다. 모델을 부르지 않는다.

## 2. 시점

`todaySummary`는 보드의 맨 위 칸이다(`leftover`와 같은 층). 항상 들어 있고, 채워지는 시점은 `leftover`와 같다.

| 상황 | `todaySummary` |
|---|---|
| 종료 버튼 요청(`end-requests` `request`)이 없거나 이어서 논의하기(`cancel`)로 취소되었고, 회의도 끝나지 않음 | `null` |
| 종료 버튼 요청을 받음 | 객체 (`final: false`). 받은 줄을 모두 처리하고 마지막 판정(flush)을 한 뒤의 값 |
| 이어서 논의하기를 받음 | 다음 보드에서 `null`로 돌아간다 |
| 회의가 끝남(`end`) | 객체 (`final: true`), 마지막 값. 요청 없이 `end`만 와도 들어간다 |

- 종료 버튼 요청과 마지막 판정(flush)은 `agenda-leftover-contract.md` 2장·5장 그대로다.
- 정리본을 만들다 실패하면 그 보드의 `todaySummary`만 `null`로 나가고 보드의 나머지(미결 안건 목록 포함)는 그대로 나간다. 다음 보드에서 다시 만든다.
- 안건 목록을 받기 전에는 보드가 나가지 않는다.
- 백엔드는 `final: true`인 값을 저장해 쓰면 된다. 회의가 끝난 뒤(`end` 이후) ai-service는 고침을 받지 않으므로 점검에서 고친 내용은 백엔드의 복사본에 반영한다.

## 3. 칸의 모양

```json
"todaySummary": {
  "requestRev": 3,
  "final": false,
  "agendas": [
    {
      "agendaId": 101,
      "status": "DECIDED",
      "statusReason": {
        "text": "...",
        "evidenceSeqs": [
          43,
          45
        ]
      },
      "decisionPoints": [
        {
          "decisionPointId": 7,
          "status": "DECIDED",
          "decisionText": "...",
          "evidenceSeqs": [
            43,
            45
          ],
          "edited": false,
          "needsCheck": false
        }
      ],
      "summary": [
        {
          "id": "k3f9a1-1",
          "text": "...",
          "evidenceSeqs": [
            42,
            43,
            45
          ],
          "edited": false,
          "needsCheck": false
        }
      ],
      "opinions": [
        {
          "id": "k3f9a1-o1",
          "kind": "OPINION",
          "text": "...",
          "evidenceSeqs": [
            42
          ],
          "edited": false,
          "needsCheck": false
        }
      ],
      "todos": [
        {
          "id": "k3f9a1-t1",
          "task": "...",
          "assigneeName": "...",
          "assigneeStatus": "MATCHED",
          "assigneeUserId": 7,
          "dueText": "...",
          "evidenceSeqs": [
            46
          ],
          "edited": false,
          "needsCheck": false,
          "unclear": false
        }
      ]
    },
    {
      "agendaId": 102,
      "status": "UNDECIDED",
      "statusReason": {
        "text": "...",
        "evidenceSeqs": [
          51
        ]
      },
      "decisionPoints": [
        "..."
      ],
      "summary": [
        "..."
      ],
      "opinions": [
        "..."
      ],
      "todos": [
        "..."
      ]
    },
    {
      "agendaId": 103,
      "status": "NOT_STARTED",
      "statusReason": null,
      "decisionPoints": [
        {
          "decisionPointId": 13,
          "status": "NOT_STARTED",
          "decisionText": "...",
          "evidenceSeqs": [],
          "edited": false,
          "needsCheck": false
        }
      ],
      "summary": [],
      "opinions": [],
      "todos": []
    }
  ]
}
```

(`...`는 줄여 쓴 자리다. 전체 예시는 `examples/api/agenda-board-today-summary-put.json`)

| 값 | 뜻 |
|---|---|
| `todaySummary: null` | 2장의 "없음" 상황 |
| `requestRev` | 마지막으로 반영한 종료 버튼 요청의 `rev`. 요청 없이 `end`만 온 경우 `null` (`leftover`와 같은 값) |
| `final` | 회의가 끝났다. 이때의 값이 마지막이다 |
| `agendas` | 안건마다 한 칸. **보드 `agendas[]`와 같은 순서**이고 안건 목록의 모든 안건이 들어 있다 |

### 3.1 안건 한 칸 (`agendas[]`)

| 칸 | 타입 | 뜻 |
|---|---|---|
| `agendaId` | 보드와 같음 | 안건 번호. 받은 타입 그대로다 |
| `status` | 문자열 | `NOT_STARTED`, `UNDECIDED`, `DECIDED`, `DECISION_CHECK` 중 하나. **종료를 확정한다고 가정한 값**이다 |
| `statusReason` | 객체 또는 null | `{text, evidenceSeqs}`. `NOT_STARTED`면 `null`. 아래 3.2 |
| `summary[]` | 배열 | 같은 시점 보드의 요약 문장(AI-03) 사본. `id`, `text`, `evidenceSeqs`, `edited`, `needsCheck` |
| `opinions[]` | 배열 | 같은 시점 보드의 의견(AI-05) 사본. `id`, `kind`, `text`, `evidenceSeqs`, `edited`, `needsCheck` |
| `todos[]` | 배열 | 같은 시점 보드의 할 일(AI-05) 사본. `agenda-opinions-contract.md` 4.2의 칸 그대로 |
| `decisionPoints[]` | 배열 | 같은 시점 보드의 결정할 사항별 결과(AI-04) 사본. `decisionPointId`, `status`, `decisionText`, `evidenceSeqs`, `edited`, `needsCheck` (`agenda-status-contract.md` 3.1). 안건 목록의 모든 사항이 들어 있다 |

- **`DISCUSSING`은 오지 않는다.** 보드에서 논의 중인 안건도 종료를 가정하면 `UNDECIDED`다. 회의가 끝나면(`final: true`) 보드의 상태와 같아진다.
- 안건 상태는 사항 상태에서 정한다 (`agenda-status-contract.md` 2.2). 결정된 사항과 미결 사항은 `decisionPoints[].status`로 나눈다. 안건이 미결이어도 그 안에 결정된 사항이 있을 수 있다.
- 결정이 아닌 안건(`UNDECIDED`, `NOT_STARTED`, `DECISION_CHECK`)에도 결정할 사항·요약·의견·할 일이 모두 들어 있다. 이월 후보는 이 상태로 알 수 있고, 이월 후보 목록과 논의 요약 사본은 `leftover`가 따로 담는다 (결정이 아닌 안건의 `status`, `statusReason`은 `leftover.agendas`와 같다).
- 담당자·기한 미정은 `assigneeName`이나 `dueText`가 `null`이거나 비어 있는 것으로 안다. 이름이 있으나 명단과 맞추지 못한 경우(`AMBIGUOUS`, `NOT_IN_ROSTER`)는 미정이 아니다. 이름을 보여 주고 `assigneeStatus`로 확인 표시를 하면 된다.
- 사람이 고친 문장·항목은 `edited`, `needsCheck`가 그대로 담긴다.
- 안건 제목은 담지 않는다. 백엔드가 가진 안건 목록에서 `agendaId`로 찾는다. `meetingId`도 보드 본문에 없으므로 백엔드가 붙인다.
- 모든 값은 복사본이다. 이후 보드가 바뀌어도 이미 만든 정리본의 값은 그대로이고, 바뀐 내용은 새 보드로 온다.
- 요청을 받은 뒤에도 새 줄이 들어오면 정리본이 바뀐다. 바뀔 때만 보드를 다시 보낸다 (`agenda-summary-contract.md` 전송 규칙 그대로).

### 3.2 결정 문장과 `statusReason`

- **결정 문장은 사항마다 `decisionPoints[].decisionText`다.** 안건 단위 결정 문장(`decision`)은 없다. 회의록 초안(AI-10)은 `DECIDED` 사항의 `decisionText`와 `evidenceSeqs`를 재료로 쓴다. 이 값은 보드의 사본이라 이 기능이 새로 쓰지 않는다. "새로 요약하지 않는다"는 원칙은 그대로다.
- cn 팀은 "결정 문장을 만들지 않고 `statusReason`으로 대신하는 안은 받기 어렵다"고 답했다. 그래서 `statusReason`이 아니라 사항별 `decisionText`를 담는다.
- `DECISION_CHECK` 사항의 `decisionText`는 결정으로 보이는 내용(후보)이고, `UNDECIDED` 사항의 것은 아직 정해지지 않은 내용이다. 모델이 쓰고 검증을 거친 문장이며, 검증에서 탈락하면 고정 문구다 (`agenda-status-contract.md` 6장).
- 안건의 `statusReason`은 코드가 만든 문장(사항 개수)이다 (`agenda-status-contract.md` 3.2). 내용으로 쓰지 않고 `decisionPoints`를 쓴다.
- 점검 화면에서 사람이 결정 내용을 고치면 그 사항의 `decisionText`가 고친 글이 되고 `edited`가 `true`다 (회의 중에는 `decision-edits`, `end` 뒤에는 백엔드 복사본에 반영한다). 사람이 미결 사항의 상태를 결정으로 바꾸는 것은 받지 않는다 (`agenda-status-contract.md` 11장).

## 4. 이 기능이 새로 만들지 않는 것

- 요약 문장, 의견, 할 일, 판정 근거, 결정 문장을 새로 쓰거나 다듬지 않는다.
- 안건의 순서를 바꾸지 않는다 (보드 순서).
- 담당자·기한을 추측해 채우지 않는다. 날짜로 바꾸지도 않는다 (`dueText`는 말한 그대로).

## 5. 상태 조회

`GET /internal/meetings/{id}/status`에 `agendaTodaySummary`가 있다. 개수만 담고 회의 내용, 안건 제목, 문장은 담지 않는다.

```json
"agendaTodaySummary": { "built": true, "agendas": 3, "decided": 1, "todos": 2, "failures": 0 }
```

| 값 | 뜻 |
|---|---|
| `built` | 마지막으로 낸 보드에 정리본이 있었다 |
| `agendas`, `decided`, `todos` | 마지막 정리본의 안건 수, 그중 `DECIDED`인 안건 수, 할 일 수 (없으면 0) |
| `failures` | 정리본을 만들다 실패한 횟수 |

## 6. 설정, 지표

새 환경변수, 새 주소, 새 `ai.task` 값, 모델 호출이 없다.

## 7. 복구

AI 서버가 재시작하면 정리본은 메모리에서 사라진다. `leftover`와 같은 재료(종료 버튼 요청 상태)로 만들므로, 마지막 종료 버튼 요청을 다시 보내 주는 복구(`agenda-leftover-contract.md` 8장)를 하면 정리본도 같이 돌아온다.

## 8. 이번 범위에서 하지 않는 것

| 하지 않는 것 | 이유 |
|---|---|
| 안건 단위 결정 문장(`decision`)과 결정의 맥락 문장 | 사항별 결정 내용(`decisionText`)은 보드의 사본으로 담는다. 맥락 문장은 새로 만들지 않고 초안(AI-10)이 요약, 의견으로 쓴다 |
| 사용자가 고른 "다음 회의로 넘기기" 선택 반영 | AI 서버는 모른다. 백엔드가 `leftover`와 합친다 |
| 정리본 저장·1년 보관 | 백엔드 몫이다 (AI 서버는 저장하지 않는다) |
| 점검 화면에서 고친 내용 받기 | `end` 뒤에는 받지 않는다. 백엔드의 복사본에 반영한다. 회의 중(종료 요청~`end`)의 고침은 기존 주소를 쓴다 (`agenda-summary-edits`, `agenda-item-edits`) |
| 기한을 날짜로 바꾸기(`dueDate`) | 말한 그대로(`dueText`)만 준다. 해석 오류를 피하려는 것이다 |
| 전사 전체를 받아 동기로 답하는 방식 (연동 API 초안 #6) | 새로 요약하지 않는다는 원칙과 맞지 않아 이 칸으로 대신하는 방식을 제안한다 |

한계

- 정리본의 품질은 안건별 요약(AI-03), 상태 판정(AI-04), 의견·할 일(AI-05)을 따른다. 입력이 된 결과의 오류는 그대로 담긴다.
- AI-04 평가에서 모델이 `DECIDED`를 한 번도 내지 않았다. 그래서 `DECIDED` 안건이 거의 없을 수 있고, 회의록의 "결정 사항"이 비어 보일 수 있다 (AI-04의 한계. 새 회의로 다시 잰다).
- 요청이 도착한 시점에 백엔드가 아직 보내지 않은 줄(최대 약 10초)은 반영되지 않는다 (`agenda-leftover-contract.md`와 같다).
- 종료 요청을 받으면 마지막 판정(flush)에 모델 호출이 들어가 정리본이 몇 초에서 수십 초 뒤에 도착할 수 있다 (추정. 응답을 기다리는 방식이 아니라 보드로 온다).
- 정합성 점검은 회의 A·B의 확정 정답(의견·할 일)과 초안 상태 정답으로 했다. 이 정답에는 할 일의 담당자 이름·기한이 하나도 없어서 이름·기한 값이 그대로 복사되는지는 모듈 자체 시험의 합성 데이터로 확인했다.

## 9. 협의 상태

| 항목 | 상태 |
|---|---|
| 안건별 묶음 모양(`agendas[]`)을 회의록 초안 요청의 `summary`로 쓴다 | 회의록 초안 담당과 합의했던 "`decision` 없이 `statusReason`을 재료로" 방식은 cn 팀이 받기 어렵다고 답해 사항별 `decisionText`로 바뀐다. **회의록 초안 담당 확인 필요**, cn 팀 확인 필요 |
| 안건 한 칸에 `decisionPoints[]`(보드 사본)를 담는다 | cn 팀이 요청한 모양의 사본. 담는 위치는 AI 제안, cn 팀 확인 필요 |
| 보드 `todaySummary` 칸의 모양과 `null` 규칙 | AI 제안, cn 팀 확인 필요. 화면 확정 전의 임시 모양 |
| 백엔드가 `final: true` 값을 저장하고 `requestRev`, `final`을 빼고 `meetingId`를 붙여 `summary`로 쓴다 | cn 팀 확인 필요 |
| 정리본 저장과 1년 보관, 점검에서 고친 내용의 반영을 cn 팀이 맡는다 | cn 팀 확인 필요 |
| 사용자의 이월 선택과 `leftover`를 합치는 방법 | cn 팀 확인 필요 |
| 연동 API 초안 #6(`POST /meetings/{id}/summary`)을 이 칸으로 대신한다 | cn 팀 확인 필요 |
| 마무리 버튼(REQ-087)과 `end`의 순서 | cn 팀 확인 필요 |
| 결정 확인 필요(`DECISION_CHECK`) 안건을 어디에 보여 줄지 | 회의록 초안은 미결 쪽에 "(결정 확인 필요)"로 넣을 계획. 정리본 화면은 기획 확인 필요 |
| 점검 화면에서 사람이 결정/미결을 정할 수 있는지, 그때 결정 문장 입력 | 화면 확정 뒤 기획·회의록 초안 담당과 확인 |
| 정리본에서 참석자가 확인·수정할 수 있는 범위 | 기획 확인 필요 |
| 담당자·기한이 없을 때의 표기 | 회의록 초안은 "담당자 미정", "기한 미정"으로 쓴다. 정리본 화면은 기획 확인 필요 |

## 10. 예시 파일

| 파일 | 내용 |
|---|---|
| `examples/api/agenda-board-today-summary-put.json` | 안건 보드 한 번의 본문 (종료 버튼 요청을 받은 상태. 안건 3개: 결정 1, 미결 1, 아직 안 다룸 1, 할 일 2건). `decisionPoints`가 없는 옛 모양이며 구현 때 3장의 새 모양으로 바꾼다 |

예시의 내용은 형식을 보여 주기 위해 만든 것이며 실제 회의가 아니다. 예시 파일이 실제 코드의 출력과 같은지는 서버 자체 시험이 확인한다.
