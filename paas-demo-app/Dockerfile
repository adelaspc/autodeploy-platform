FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN adduser --disabled-password --gecos "" appuser

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser wsgi.py ./
COPY --chown=appuser:appuser --from=frontend-builder /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/instance /app/frontend && chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

CMD ["sh", "-c", "exec gunicorn --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --bind 0.0.0.0:${PORT:-5000} --access-logfile - --error-logfile - wsgi:app"]
