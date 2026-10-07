# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10010 app && useradd --system --uid 10010 --gid app --home /app app
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY salesagent ./salesagent

USER app
EXPOSE 8000

# Overridden per service in compose.yaml
CMD ["uvicorn", "salesagent.api.tool_api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
