FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN uv run --no-sync playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && useradd --create-home --uid 10001 agent \
    && mkdir -p /app/runs \
    && chown agent:agent /app/runs

USER agent
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn document_retrieval_agent.email.server:create_app --factory --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
