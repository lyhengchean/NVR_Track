FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Phnom_Penh
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY scripts/ ./scripts/
RUN mkdir -p /app/config /app/reports

# Optional, isolated test target. Not used by Compose services.
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
COPY config/nvrs.example.json ./config/nvrs.example.json
# Example settings only (no secrets); tests check its documented defaults.
COPY .env.example ./.env.example
ENTRYPOINT ["python", "-m", "pytest", "-q"]

FROM base AS runtime
ENTRYPOINT ["python", "-m", "scripts.run_nvr_report"]
