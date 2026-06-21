FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEPLOYMENT_NOTES_SERVE_FRONTEND=false

RUN adduser --disabled-password --gecos "" appuser

WORKDIR /app

COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY --chown=appuser:appuser backend ./backend
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser wsgi.py ./

RUN mkdir -p /app/instance && chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

CMD ["sh", "-c", "exec gunicorn --workers ${GUNICORN_WORKERS:-2} --threads ${GUNICORN_THREADS:-4} --bind 0.0.0.0:${PORT:-5000} --access-logfile - --error-logfile - wsgi:app"]
