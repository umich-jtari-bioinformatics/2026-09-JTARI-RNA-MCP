# JTARI MCP server image (HTTP mode). Pattern: https://docs.astral.sh/uv/guides/integration/docker/
# The database and the keys file are NOT in the image; compose mounts them at run time.
FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Layer 1: dependencies only, cached until uv.lock changes
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --no-install-project
# Layer 2: the project itself
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home jtari
COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    JTARI_DB=/data/jtari.duckdb \
    JTARI_MCP_KEYS_FILE=/keys/keys.json \
    JTARI_MCP_HOST=0.0.0.0 \
    JTARI_MCP_PORT=8000
USER jtari
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health').status != 200)"
CMD ["python", "-m", "jtari_mcp.server", "--http"]
