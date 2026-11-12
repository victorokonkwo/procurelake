# Task runner for Step Functions (ECS Fargate, ARM64; ADR 0040): batch ingestion and the daily
# data-quality checks. Build from the repository root:
#   docker build --platform linux/arm64 -f jobs/containers/task_runner/Dockerfile -t task-runner .
# Same pinned base images as the dbt runner (jobs/containers/dbt_runner/Dockerfile). Python
# dependencies come from uv.lock: the project's runtime dependencies only (no dev, ai or dbt).
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.11.15-slim-trixie@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff
RUN apt-get update \
 && apt-get upgrade --yes --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-default-groups --no-install-project && rm -rf /root/.cache
# The wheel bundles contracts/source and configs/pipelines/silver (pyproject force-include).
COPY src/ src/
COPY contracts/source/ contracts/source/
COPY configs/pipelines/silver/ configs/pipelines/silver/
RUN uv sync --locked --no-default-groups --no-editable && rm -rf /root/.cache src contracts configs

COPY jobs/containers/task_runner/entrypoint.py /app/entrypoint.py

# Unprivileged runtime user; downloads go to /tmp (Fargate ephemeral storage).
RUN useradd --system --uid 10001 runner
USER runner
ENTRYPOINT ["python", "/app/entrypoint.py"]
