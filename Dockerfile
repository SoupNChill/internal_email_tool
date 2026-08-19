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

# §19: no unnecessary packages. This container never installs anything, so the
# installers are pure attack surface. Removed after the venv is copied so the
# build stage keeps them.
RUN /venv/bin/pip uninstall -y pip setuptools wheel 2>/dev/null || true; \
    rm -rf /venv/lib/python3.12/site-packages/pip* \
           /venv/lib/python3.12/site-packages/setuptools* \
           /venv/lib/python3.12/site-packages/wheel* \
           /venv/lib/python3.12/site-packages/pkg_resources \
    && find /venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
    && rm -rf /root/.cache

WORKDIR /app
COPY --chown=emaild:emaild emaild ./emaild
COPY --chown=emaild:emaild alembic ./alembic
COPY --chown=emaild:emaild alembic.ini ./

# Mountpoint for the self-generating secrets volume (emaild/secretstore.py).
# It must exist IN THE IMAGE, owned by emaild: Docker copies the ownership of
# an existing directory onto a fresh named volume, and creates it root-owned if
# the path is absent. Without this the container -- which runs as uid 10001 --
# could not write the key it is supposed to generate on first boot.
RUN mkdir -p /var/lib/emaild/secrets \
    && chown -R emaild:emaild /var/lib/emaild \
    && chmod 700 /var/lib/emaild/secrets

USER emaild

# Metadata (§19). Overridden with real values by the release workflow.
ARG APP_VERSION=0.0.0-dev  # overridden by the release workflow
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
