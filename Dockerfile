# syntax=docker/dockerfile:1
#
# One image definition for every Railway service. SERVICE_ROLE selects extras:
#   web       FastAPI API + Jinja PWA (default)
#   pipeline  ingest / notify workers and the outbox relay (OCR, no audio stack)
#   worker    meeting worker: adds ffmpeg and faster-whisper
#             (WITH_DIARIZATION=1 also adds pyannote.audio for speaker identification)
#
# Railway passes service variables to declared ARGs, so set SERVICE_ROLE=worker
# on worker services (see .railway/railway.ts). Locally:
#   docker build -t sonali-archive-web .
#   docker build --build-arg SERVICE_ROLE=worker -t sonali-archive-worker .
#   docker build --build-arg SERVICE_ROLE=worker --build-arg WITH_DIARIZATION=1 -t sonali-archive-worker-dz .

# React PWA served by FastAPI under /app (see src/bcp_project/spa.py and docs/FRONTEND.md).
FROM node:26-bookworm-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS deps

ARG SERVICE_ROLE=web
ARG WITH_DIARIZATION=0
ENV PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-worker.txt requirements-diarization.txt ./
RUN pip install --upgrade pip \
    && pip install --prefix=/install -r requirements.txt \
    && if [ "$SERVICE_ROLE" = "worker" ]; then pip install --prefix=/install -r requirements-worker.txt; fi \
    && if [ "$SERVICE_ROLE" = "worker" ] && [ "$WITH_DIARIZATION" = "1" ]; then \
         pip install --prefix=/install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-diarization.txt; fi

FROM python:3.12-slim-bookworm

ARG SERVICE_ROLE=web
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    LOG_FORMAT=json \
    UPLOAD_DIR=/data/uploads \
    MINUTES_BANGLA_FONT=/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf \
    PYTHONPATH=/app/src \
    SERVICE_ROLE=${SERVICE_ROLE} \
    PORT=8080 \
    HOST=0.0.0.0 \
    METRICS_MULTIPROC_DIR=/tmp/bcp-metrics

WORKDIR /app

# Bangla + English Tesseract data are both required for bilingual OCR.
# Bengali + DejaVu TrueType fonts are for approved-minutes PDFs (shaped with uharfbuzz).
# The base image lags Debian security fixes; upgrading here keeps the CI image scan green.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    curl \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ben \
    fonts-lohit-beng-bengali \
    fonts-dejavu-core \
    && if [ "$SERVICE_ROLE" = "worker" ]; then apt-get install -y --no-install-recommends ffmpeg; fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /install /usr/local
COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY src ./src
COPY --from=frontend /frontend/dist ./frontend/dist
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN mkdir -p /data/uploads/meetings \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data \
    && chmod +x /docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD [ "$SERVICE_ROLE" != "web" ] || curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]
# Railway injects PORT at runtime — always bind 0.0.0.0 so the edge proxy can reach us.
# Worker services override this with their start command.
CMD ["sh", "-c", "exec uvicorn bcp_project.main_api:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-*}\" --workers ${WEB_CONCURRENCY:-2}"]
