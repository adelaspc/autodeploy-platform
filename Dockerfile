FROM node:26-bookworm-slim@sha256:367679cf9792759492a486e4aa4b421764d71a9546a6dae8aab81a99eb797b3e AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM alpine/helm:4.3.1@sha256:e3f8d5c462d75470fbb1fce79ad5c20063657096d1f98c0fcea869e2600e2e5e AS helm-cli

FROM python:3.15-slim-trixie@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913 AS runtime
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
