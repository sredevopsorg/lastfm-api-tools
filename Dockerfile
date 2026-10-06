# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the SPA
# ---------------------------------------------------------------------------
FROM docker.io/library/node:22-alpine AS web
WORKDIR /web
RUN corepack enable
ENV npm_config_store_dir=/pnpm-store
# pnpm-workspace.yaml carries the allowBuilds policy: esbuild's install script is
# approved deliberately (pnpm 11+ blocks dependency scripts by default) because
# Vite needs the platform binary it places.
COPY apps/web/package.json apps/web/pnpm-workspace.yaml apps/web/pnpm-lock.yaml* ./
# The lockfile is committed, so a cached, reproducible install is the only
# acceptable outcome here. Falling back to a resolving install would silently
# build a different dependency tree than the one that was tested.
RUN --mount=type=cache,target=/pnpm-store pnpm install --frozen-lockfile
COPY apps/web/ ./
RUN pnpm run build

# ---------------------------------------------------------------------------
# Stage 2: resolve Python dependencies
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.12-slim AS pybuild
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml README.md ./
# Dependency layer first so source edits do not invalidate it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-install-project --no-dev --no-editable
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    WEB_DIST_DIR=/app/web

RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --home-dir /app --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=pybuild --chown=10001:10001 /app/.venv /app/.venv
COPY --from=pybuild --chown=10001:10001 /app/src /app/src
COPY --from=pybuild --chown=10001:10001 /app/migrations /app/migrations
COPY --from=pybuild --chown=10001:10001 /app/alembic.ini /app/alembic.ini
COPY --from=web --chown=10001:10001 /web/dist /app/web

USER 10001:10001
EXPOSE 8080

# Liveness only: must not depend on Postgres or Jellyfin.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "metaedit.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
