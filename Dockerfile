FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM alpine/helm:4.2.4@sha256:76c375eed56144c68d6197c55bc5a4552fb42002190b796729901cbab3ae6e51 AS helm-cli

FROM python:3.14-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --from=helm-cli /usr/bin/helm /usr/local/bin/helm

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
        docker-cli \
        docker-buildx \
        git \
        kubernetes-client \
    && rm -rf /var/lib/apt/lists/* \
    && helm version --short \
    && adduser --disabled-password --gecos "" appuser

WORKDIR /app

COPY requirements.lock.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes -r requirements.lock.txt

COPY --chown=appuser:appuser control_plane ./control_plane
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser worker ./worker
COPY --chown=appuser:appuser deploy/helm/generic-web-app ./deploy/helm/generic-web-app
COPY --chown=appuser:appuser wsgi.py ./
COPY --chown=appuser:appuser --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/instance /app/frontend /tmp/paas-workspaces \
    && chown -R appuser:appuser /app /tmp/paas-workspaces

USER appuser

EXPOSE 5000

CMD ["sh", "-c", "exec gunicorn --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --bind 0.0.0.0:${PORT:-5000} --access-logfile - --error-logfile - wsgi:app"]
