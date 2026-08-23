# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    UPLOAD_DIR=/data/uploads \
    PORT=8080 \
    HOST=0.0.0.0

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    poppler-utils \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY pyproject.toml .
COPY scripts ./scripts
COPY src ./src
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN mkdir -p /data/uploads/meetings \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data \
    && chmod +x /docker-entrypoint.sh

# Start as root so entrypoint can chown the mounted volume, then drop to appuser.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
# Railway injects PORT at runtime — always bind 0.0.0.0 so the edge proxy can reach us.
CMD ["sh", "-c", "exec uvicorn src.bcp_project.main_api:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
