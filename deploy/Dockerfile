# One image (nvr-track:local) for every service in compose.yaml.
# Build context: the repository root (compose.yaml: context ".", dockerfile deploy/Dockerfile).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    TZ=Asia/Phnom_Penh
# /app is the project root inside the container: config/, reports/ and state/
# are mounted here by compose.yaml, the code is in backend/, the pages in frontend/.
WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/nvr_track/ ./backend/nvr_track/
COPY frontend/ ./frontend/
RUN mkdir -p /app/config /app/reports

# Optional, isolated test target. Not used by Compose services.
FROM base AS test
COPY backend/requirements-dev.txt ./backend/requirements-dev.txt
RUN pip install --no-cache-dir -r backend/requirements-dev.txt
COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/tests/ ./backend/tests/
COPY config/nvrs.example.json ./config/nvrs.example.json
# Example settings only (no secrets); tests check its documented defaults.
COPY .env.example ./.env.example
COPY compose.yaml ./compose.yaml
COPY scripts/ ./scripts/
ENTRYPOINT ["python", "-m", "pytest", "-q", "backend/tests"]

FROM base AS runtime
ENTRYPOINT ["python", "-m", "nvr_track.cli.run_nvr_report"]
