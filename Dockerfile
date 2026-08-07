# release_rules §19: explicit base version, no dev tools in the final image,
# non-root, minimal surface. Pinned by digest-able tag rather than `latest`.
FROM python:3.12.8-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- build stage: compilers live here and never reach the runtime image ---
FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY emaild ./emaild

RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip setuptools wheel \
    && /venv/bin/pip install .

# --- runtime ---
FROM base AS runtime

# Non-root. Fixed uid so bind-mounted volumes have predictable ownership.
RUN groupadd --gid 10001 emaild \
    && useradd --uid 10001 --gid emaild --create-home --shell /usr/sbin/nologin emaild

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app
COPY --chown=emaild:emaild emaild ./emaild
COPY --chown=emaild:emaild alembic ./alembic
COPY --chown=emaild:emaild alembic.ini ./

USER emaild

# Metadata (§19). Overridden with real values by the release workflow.
ARG APP_VERSION=0.1.0
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown

# Surfaced through /version so a running container is traceable to its source.
ENV EMAILD_GIT_COMMIT=${GIT_COMMIT} \
    EMAILD_BUILD_TIME=${BUILD_TIME}

LABEL org.opencontainers.image.title="emaild" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.source="internal"

EXPOSE 8000

# Bind to all interfaces INSIDE the container only; compose decides whether the
# port is published to the host. §46: exposure is a deployment decision.
CMD ["uvicorn", "emaild.main:app", "--host", "0.0.0.0", "--port", "8000"]
