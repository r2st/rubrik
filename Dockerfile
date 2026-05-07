# Multi-stage build for the Transcript Intelligence API.
# Stage 1 builds wheels, stage 2 ships only the runtime artifacts.

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder
WORKDIR /build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-deps --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    PORT=8000

# Install runtime deps from prebuilt wheels
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels

# Copy app code, migrations, and bootstrap config the loader expects
COPY src ./src
COPY api ./api
COPY web ./web
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY bootstrap.toml.example ./bootstrap.toml.example

# Entrypoint:
#   RUN_MIGRATIONS=auto (default) — apply alembic migrations on start, then
#     exec uvicorn. Right for single-replica dev / docker compose; idempotent
#     and Alembic locks safely.
#   RUN_MIGRATIONS=skip — skip the alembic step. Right for production rolling
#     deploys where a Helm pre-upgrade Job (deploy/k8s/migrate-job.yaml) has
#     already run migrations to completion before the rolling update starts.
# Override via the [runtime] table in bootstrap.toml or the env var directly.
COPY <<'EOF' /app/entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail
mode="${RUN_MIGRATIONS:-auto}"
if [[ "$mode" == "auto" ]]; then
  echo "[entrypoint] RUN_MIGRATIONS=auto — applying alembic migrations..."
  alembic upgrade head
elif [[ "$mode" == "skip" ]]; then
  echo "[entrypoint] RUN_MIGRATIONS=skip — assuming migrations applied via Job"
else
  echo "[entrypoint] RUN_MIGRATIONS=$mode is invalid (use auto|skip)" >&2
  exit 2
fi
echo "[entrypoint] starting uvicorn..."
exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "${WORKERS:-2}"
EOF
RUN chmod +x /app/entrypoint.sh

# Non-root user
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status == 200 else 1)"

CMD ["/app/entrypoint.sh"]
