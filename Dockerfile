FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저(캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config.yaml .

# 상태/로그 영속 디렉터리 (compose 에서 볼륨 마운트)
RUN mkdir -p /app/data

# 종료 시 SIGTERM 으로 graceful shutdown (main 이 상태 저장 후 종료)
STOPSIGNAL SIGTERM

CMD ["python", "-m", "src.main"]
