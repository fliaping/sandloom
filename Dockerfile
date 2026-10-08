ARG BASE_IMAGE=agent-sandbox-base:latest
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="Sandloom"

WORKDIR /app
ENV PYTHONPATH=/app/src \
    SANDBOX_BUBBLEWRAP_PATH=/usr/bin/bwrap \
    SANDBOX_SETPRIV_PATH=/usr/bin/setpriv \
    SANDBOX_PRLIMIT_PATH=/usr/bin/prlimit \
    SANDBOX_LOCAL_ROOT=/var/lib/agent-sandbox/sandboxes

COPY pyproject.toml uv.lock README.md ./
COPY runtime ./runtime
COPY src ./src
COPY start.sh ./start.sh

# Every adapter extra is installed, not just the default SQLite path. The image
# is the deployment artifact, and the README describes selecting PostgreSQL,
# MySQL, Redis, or S3 purely through environment variables. Shipping without the
# drivers turns that into a startup crash — `ModuleNotFoundError: asyncpg` — for
# a configuration the documentation presents as supported. A few megabytes is
# the cheaper mistake.
#
# `--frozen` installs exactly `uv.lock`, which is what `Dockerfile.test` does
# and what the integration suite therefore exercises. Resolving from the index
# at build time instead would put a dependency set in the artifact that no test
# has ever run: the image built that way carried redis 8.1.0, starlette 1.7.0,
# and sqlalchemy 2.1.1 while the suite ran 4.6.0, 1.3.1, and 2.0.51 — and two
# tests fail on the former. It also makes a build reproducible, which is what
# an SBOM or a signature is a statement about.
RUN uv sync --frozen --no-dev --no-install-project --all-extras \
    && chmod 0755 /app/start.sh \
    && python -c "import agent_sandbox, agent_sandbox_runtime" \
    && python -c "import asyncpg, aiomysql, redis, boto3"

VOLUME ["/var/lib/agent-sandbox"]
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SANDBOX_PORT:-8080}/healthz" || exit 1

CMD ["/app/start.sh"]
