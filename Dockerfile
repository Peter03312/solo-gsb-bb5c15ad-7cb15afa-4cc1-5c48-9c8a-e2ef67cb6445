FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/allergen.db \
    APP_PORT=8000

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# SQLite 数据目录（可挂载卷持久化）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 启动即自动建表（幂等），无需额外初始化步骤
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT}"]
