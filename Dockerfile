FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM alpine/helm:4.2.0 AS helm-cli

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY --from=helm-cli /usr/bin/helm /usr/local/bin/helm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        docker.io \
        docker-cli \
        docker-buildx \
        git \
        kubernetes-client \
    && rm -rf /var/lib/apt/lists/* \
    && helm version --short \
    && adduser --disabled-password --gecos "" appuser

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

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
