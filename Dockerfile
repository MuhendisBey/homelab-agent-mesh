# ============================================================================
# homelab-agent-mesh - Lightweight Multi-Stage Dockerfile (<80 MB RSS target)
# ============================================================================

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1 \
    MALLOC_ARENA_MAX=2 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --home-dir /app agent \
    && mkdir -p /app/data /app/config \
    && chown -R agent:agent /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=agent:agent src ./src
COPY --chown=agent:agent config/settings.example.yaml ./config/settings.example.yaml

USER agent

EXPOSE 8080

CMD ["homelab-orchestrator"]

