# 웹 서버와 워커가 같이 쓰는 이미지 (11교시~). 실행 명령만 docker-compose.yml에서 다르게 준다.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lesson11 ./lesson11
WORKDIR /app/lesson11
