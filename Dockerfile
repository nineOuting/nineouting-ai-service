FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 층 순서는 잘 안 바뀌는 것부터. 코드만 고치면 의존성과 모델을 다시 받지 않는다

# 1) 요약 서버 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) RAG 의존성. torch 는 CPU 전용 색인에서 먼저 받는다 (기본 색인은 GPU 용 파일까지 받아 이미지가 크게 늘어남)
COPY rag/requirements.txt rag/requirements.txt
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r rag/requirements.txt

# 2-1) 회의록 초안 의존성. 지금은 RAG 와 같은 패키지라 새로 받는 것은 없다
COPY minutes/requirements.txt minutes/requirements.txt
RUN pip install --no-cache-dir -r minutes/requirements.txt

# 3) 임베딩 모델. revision 고정으로 받고 sha256 을 확인한다. 실행 사용자가 바뀌어도 같은 곳을 찾도록 /models 로 고정
ENV HF_HUB_CACHE=/models
COPY rag/__init__.py rag/config.py rag/embedding.py rag/fetch_model.py rag/
RUN python -m rag.fetch_model && chmod -R a+rX /models
ENV HF_HUB_OFFLINE=1

# 4) 코드 (커밋마다 바뀌는 부분이라 맨 뒤)
COPY summary/ summary/
COPY server/ server/
COPY rag/ rag/
COPY minutes/ minutes/

# root 가 아닌 사용자로 실행. 서버 경로는 파일을 쓰지 않는다
RUN useradd --uid 10001 --create-home app
USER 10001

# 비밀키와 설정은 이미지에 넣지 않고 실행할 때 환경변수로 받는다
# RAG 는 RAG_DATABASE_URL 이 있을 때만 켜진다. 처음 배포 전에 같은 이미지로 python -m rag.migrate 를 한 번 돌린다
# 회의록 초안은 MINUTES_DATABASE_URL 이 있을 때만 켜진다. 마찬가지로 python -m minutes.migrate 를 한 번 돌린다
EXPOSE 8000
# RAG 를 켜면 시작할 때 모델을 불러오므로 start-period 를 둔다
HEALTHCHECK --interval=30s --timeout=3s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
