FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CONFIG_PATH=/app/config.yaml \
    GDRIVE_CREDENTIALS_PATH=/data/gdrive_credentials.json \
    GDRIVE_TOKEN_PATH=/data/gdrive_token.json

WORKDIR /app

# Install system dependencies for Pillow/lxml/trafilatura and cron scheduling
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    fonts-dejavu-core \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml AGENTS.md config.yaml ./
COPY src ./src

RUN uv pip install --system --no-cache . && mkdir -p /data

ENTRYPOINT ["the-reading"]
CMD []
