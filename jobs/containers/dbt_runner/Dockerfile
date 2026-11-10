# dbt runner for Step Functions (ECS Fargate, ARM64; ADR 0029). Build from the repository root:
#   docker build --platform linux/arm64 -f jobs/containers/dbt_runner/Dockerfile -t dbt-runner .
# Base images are pinned by digest; Python dependencies come from uv.lock (dbt group only).
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

# Debian 13 (trixie) base. The first image, on bookworm (Debian oldstable), had 10 critical and 57
# high OS-package findings in the ECR scan (openssl, perl, gnutls, krb5, glibc, 2026-11-10).
FROM python:3.11.15-slim-trixie@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff
# Apply pending Debian security updates at build time (the base digest alone lags them).
RUN apt-get update \
 && apt-get upgrade --yes --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv PATH=/app/.venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --only-group dbt --no-install-project && rm -rf /root/.cache

COPY dbt/ dbt/
COPY jobs/containers/dbt_runner/entrypoint.py /app/entrypoint.py

# Unprivileged runtime user; dbt writes only under /tmp (target, logs).
RUN useradd --system --uid 10001 dbt
USER dbt
ENV DBT_LOG_PATH=/tmp/dbt-logs
ENTRYPOINT ["python", "/app/entrypoint.py"]
