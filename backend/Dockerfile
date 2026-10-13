FROM python:3.13-slim

# A pinned uv binary keeps image builds reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    # Services start with `uv run ...`; never re-resolve or install at runtime.
    UV_FROZEN=1 \
    UV_NO_SYNC=1

WORKDIR /app

# curl for the compose healthchecks; borgbackup for archive retention.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl borgbackup \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code changes reuse the cached layer.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Run as a non-root user (this image holds the Fernet key that decrypts pooled-account OAuth tokens).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app /opt/venv
USER appuser
