FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QBOT_CONFIG_PATH=/config/config.json

WORKDIR /app
COPY api ./api
COPY core ./core
COPY handlers ./handlers
COPY locales ./locales
COPY monitor ./monitor
COPY telegram ./telegram
COPY main.py ./

RUN adduser -D -H qbot
USER qbot

CMD ["python3", "main.py"]