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
- SQLite fallback for local bootstrapping when `DATABASE_URL` is not set

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

Copy the example value from [`.env.example`](.env.example) and set your database URL.

Example:

```bash
DATABASE_URL=mysql+pymysql://app_user:app_password@db:3306/deployments
```

If `DATABASE_URL` is not set, the app falls back to SQLite:

```text
sqlite:///deployments.db
```

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

- The Flask backend does not define a route for `/`, so a `404` on `http://127.0.0.1:5000/` is expected.
- The frontend is the main user-facing entrypoint during local development.
- The current backend returns JSON for normal API operations and validation errors.
