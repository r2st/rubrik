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

# Entrypoint: run migrations to head, then exec uvicorn. Migrations are
# idempotent so this is safe on every container start (single-replica
# deployments) and harmless under multiple replicas (Alembic takes a lock).
COPY <<'EOF' /app/entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail
echo "[entrypoint] applying alembic migrations..."
alembic upgrade head
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
