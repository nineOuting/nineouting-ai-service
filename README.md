# nineouting-ai-service

AI 회의 어시스턴트의 AI 서비스: 회의 전사문 요약 (끝난 회의 요약, 회의 중 주기 요약)

## 구성

- summary/meeting_summary.py : 끝난 회의 전사문 요약
- summary/meeting_live_summary_v2.py : 회의 중 주기 요약 (meeting_summary.py를 불러 씀)
- summary/input_adapter.py : 백엔드 입력(camelCase)을 요약 코드 내부 형식으로 변환
- server/ : 백엔드가 보내는 회의 알림과 줄 묶음을 받는 FastAPI 서버 (main.py: 주소, service.py: 회의별 상태·중복 처리)
- Dockerfile, requirements.txt, .env.example : 실행·배포용

## 실행 (비용 0인 자체 시험)

    cd summary
    python3 meeting_summary.py --selftest
    python3 meeting_live_summary_v2.py --selftest
    python3 input_adapter.py

수신 서버 (저장소 루트에서)

    pip install -r requirements.txt
    python3 -m server.selftest
    cp .env.example .env
    uvicorn server.main:app --port 8000
    curl localhost:8000/health

기본 LLM_PROVIDER=echo 라서 모델을 부르지 않는다 (비용 0).

## 주의

- Gemini API 키는 코드나 파일에 쓰지 않고 환경변수 GEMINI_API_KEY로만 설정한다
- 전사 파일과 실행 결과는 개인 대화가 담겨 있어 올리지 않는다
