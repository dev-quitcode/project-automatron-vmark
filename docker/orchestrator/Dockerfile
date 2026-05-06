# ============================================================
# Automatron Orchestrator - FastAPI + LangGraph
# ============================================================

FROM python:3.12-slim

LABEL maintainer="Automatron"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    docker.io \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY orchestrator/pyproject.toml orchestrator/README.md* ./
COPY orchestrator/orchestrator/ ./orchestrator/
COPY orchestrator/prompts/ ./prompts/
COPY orchestrator/scripts/ ./scripts/

RUN uv pip install --system -e ".[dev]"

RUN mkdir -p /app/data /var/automatron/workspaces

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "8000"]
