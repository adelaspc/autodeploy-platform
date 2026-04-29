# Deployment Notes App

Deployment Notes App is a small Flask + SQLAlchemy application for tracking deployment records in a PaaS-style demo environment. It is designed as an infrastructure and platform validation workload rather than a full end-user product.

The project includes:

- a Flask backend with REST endpoints for deployment tracking
- SQLAlchemy models and Flask-Migrate database migrations
- a Vue 3 + Vite frontend with a terminal-style interface
- health endpoints for service and database connectivity

## Purpose

This app is intended to exercise common platform concerns such as:

- containerization
- environment variable injection
- database connectivity
- migrations
- CI/test execution
- deployment lifecycle tracking
- health and observability checks

## Implemented Functionality

### Deployment management

The application can:

- create deployment records
- list deployment history
- retrieve a single deployment by ID
- update deployment status
- delete deployment records

Each deployment currently stores:

- `application_name`
- `version`
- `environment`
- `status`
- `created_at`
- `updated_at`

### Status lifecycle rules

Deployment status transitions are enforced by the backend.

Allowed transitions:

- `pending -> building`
- `pending -> failed`
- `building -> deployed`
- `building -> failed`

Terminal statuses:

- `deployed`
- `failed`

The API returns `allowed_transitions` for each deployment, and the frontend disables invalid status actions.

### Filtering and pagination

Deployment history supports:

- filtering by `environment`
- filtering by `status`
- filtering by `application_name` substring
- paginated listing with `page` and `per_page`

### Health diagnostics

The application exposes:

- `/health` for service-level health
- `/health/db` for database connectivity checks

The frontend shows:

- service status
- database status
- last health check timestamp
- database diagnostic details when the DB probe fails

## Tech Stack

### Backend

- Flask
- Flask-SQLAlchemy
- Flask-Migrate
- SQLAlchemy
- PyMySQL
- python-dotenv

### Frontend

- Vue 3
- Vite

### Database

- MySQL in the intended deployment setup
- SQLite fallback only for `APP_ENV=local|development|test` when `DATABASE_URL` is not set

## Project Structure

```text
paas-demo-app/
  backend/
    api/
    models/
    config.py
    extensions.py
    __init__.py
  frontend/
    src/
    package.json
    vite.config.js
  migrations/
  tests/
  requirements.txt
  requirements-dev.txt
  wsgi.py
```

## API Overview

### Health

- `GET /health`
- `GET /health/db`

### Deployments

- `GET /api/deployments`
- `GET /api/deployments/<id>`
- `POST /api/deployments`
- `PATCH /api/deployments/<id>`
- `DELETE /api/deployments/<id>`

### `GET /api/deployments` query parameters

- `application_name`
- `environment`
- `status`
- `page`
- `per_page`

### Example deployment payload

```json
{
  "application_name": "billing-api",
  "version": "2026.04.27-1",
  "environment": "staging",
  "status": "pending"
}
```

### Example paginated response

```json
{
  "items": [
    {
      "id": 1,
      "application_name": "billing-api",
      "version": "2026.04.27-1",
      "environment": "staging",
      "status": "pending",
      "allowed_transitions": ["building", "failed"],
      "created_at": "2026-04-27T12:03:31.603875",
      "updated_at": "2026-04-27T12:03:31.603879"
    }
  ],
  "page": 1,
  "per_page": 10,
  "total": 1,
  "pages": 1,
  "has_next": false,
  "has_prev": false
}
```

## Local Setup

### 1. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install backend dependencies

```bash
pip install -r requirements.txt
```

For tests:

```bash
pip install -r requirements-dev.txt
```

### 3. Configure environment variables

Copy [`.env.example`](.env.example) to `.env` and adjust values as needed.

For local Python-based development, a minimal setup is:

```bash
APP_ENV=development
```

If `DATABASE_URL` is not set and `APP_ENV` is `local`, `development`, or `test`, the app falls back to:

```text
sqlite:///instance/app.db
```

Outside those environments, `DATABASE_URL` is required and the app will fail fast if it is missing.

### 4. Run migrations

```bash
.venv/bin/flask --app wsgi:app db upgrade
```

### 5. Run the backend

```bash
.venv/bin/flask --app wsgi:app run
```

Backend endpoints will be available on:

```text
http://127.0.0.1:5000
```

### 6. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend UI will usually be available on:

```text
http://127.0.0.1:5173
```

The Vite dev server proxies `/api` and `/health` requests to the Flask backend.

## Docker Workflow

The repository includes a production-oriented multi-stage [Dockerfile](Dockerfile) and a local orchestration [docker-compose.yml](docker-compose.yml).

### Environment variables used by Compose

Copy [`.env.example`](.env.example) to `.env` before starting the stack. Docker Compose uses `.env` for variable interpolation, and the `app` and `db` services also load that same file with `env_file`.

The Compose stack supports these variables:

- `APP_PORT` for the host port mapped to the app container, default `5000`
- `PORT` for the internal app port, default `5000`
- `MYSQL_PORT` for the host port mapped to MySQL, default `3306`
- `MYSQL_DATABASE`, default `deployments`
- `MYSQL_USER`, default `app_user`
- `MYSQL_PASSWORD`, default `app_password`
- `MYSQL_ROOT_PASSWORD`, default `root_password`

### Start the stack

```bash
docker compose up --build
```

The app will be available on:

```text
http://127.0.0.1:${APP_PORT:-5000}
```

### Run migrations

Run schema migrations explicitly after the stack is up:

```bash
docker compose run --rm app flask db upgrade
```

### Stop the stack

```bash
docker compose down
```

To also remove the MySQL volume:

```bash
docker compose down -v
```

## Worker Testing

The current worker implementation is a control-plane skeleton. It advances deployment records through the documented states and persists deployment events, but it still uses a fake executor for clone/build/push/deploy steps.

### Run the worker once

Create a pending deployment through the API, then execute:

```bash
python -m flask --app wsgi:app run-worker-once
```

If a pending deployment exists, the worker will move it through the state machine and record deployment events. If no pending deployment exists, it prints:

```text
No pending deployments found
```

### Inspect deployment results

After running the worker, inspect the deployment and its event history through the API:

```bash
curl http://127.0.0.1:5000/api/projects/<project_id>/deployments
curl http://127.0.0.1:5000/api/projects/<project_id>/deployments/<deployment_id>/events
```

### Automated verification

Run the full test suite with:

```bash
pytest -q
```

The worker tests cover:

- successful deployment processing
- skipping the testing phase when no test command is configured
- failure handling with persisted deployment events

## Testing

Run backend tests with:

```bash
.venv/bin/python -m pytest -q
```

Build the frontend with:

```bash
cd frontend
npm run build
```

## Current UI Behavior

The frontend provides:

- deployment creation form
- health monitor panel
- deployment history table
- status transition actions
- delete action
- history filters
- history pagination

The interface intentionally uses a Linux terminal-inspired presentation for demo and observability scenarios.

## Notes

- During Vite-based local development, the frontend is the main user-facing entrypoint at `http://127.0.0.1:5173`.
- In containerized or built mode, Flask serves the compiled frontend assets from `/` when `frontend/dist` is present.
- The backend returns JSON for normal API operations and validation errors.
