# nineouting-ai-service

AI 회의 어시스턴트의 AI 서비스: 회의 전사문 요약 (끝난 회의 요약, 회의 중 주기 요약)

## 구성

- summary/meeting_summary.py : 끝난 회의 전사문 요약
- summary/meeting_live_summary_v2.py : 회의 중 주기 요약 (meeting_summary.py를 불러 씀)

## 실행 (비용 0인 자체 시험)

    cd summary
    python3 meeting_summary.py --selftest
    python3 meeting_live_summary_v2.py --selftest

## 주의

- Gemini API 키는 코드나 파일에 쓰지 않고 환경변수 GEMINI_API_KEY로만 설정한다
- 전사 파일과 실행 결과는 개인 대화가 담겨 있어 올리지 않는다
