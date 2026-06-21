# Deployment Notes App

Deployment Notes is a small full-stack deployment tracking application. It lets a user record application deployments, follow their lifecycle, filter deployment history, and confirm that the backend service and database are reachable.

The project is intentionally scoped as an independent sample workload. In the larger autodeploy platform it can represent a real user-owned repository that a platform would clone, build, and deploy. It does not import the platform control plane, worker process, or platform models, so it can also be reviewed and run as a standalone portfolio project.

![Deployment Notes deployed app](../docs/assets/deployment-notes-app.png)

## Demo Scope

This application exists to exercise the AWS infrastructure and CI/CD workflow in the parent Terraform repository. It is intentionally simple and is not production-ready application software.

Intentional limitations:

- No user authentication or authorization.
- No hardened public API security model.
- No multi-tenant data isolation.
- No rate limiting or abuse protection.
- Minimal UI focused on smoke testing the deployed stack.

The goal is to demonstrate DevOps and infrastructure engineering skills: container builds, ECR publishing, SSM-based deployment configuration, database migrations, Auto Scaling Group refreshes, ALB routing, RDS connectivity, and operational health checks.

## What the App Does

- Creates deployment records with an application name, version, target environment, and initial status.
- Displays deployment history in a Vue dashboard.
- Filters deployment history by application name, environment, and status.
- Paginates deployment history so the API remains predictable as the table grows.
- Updates deployment status through explicit lifecycle transition rules.
- Deletes deployment records.
- Exposes health checks for the Flask service and database connection.
- Serves the compiled Vue frontend from the Flask application in production.
- Supports local SQLite development and containerized MySQL deployment.

## Feature Walkthrough

### Deployment Creation

The UI includes a form for creating a deployment record. Each deployment includes:

- `application_name`: the service or application being deployed, for example `billing-api`.
- `version`: the release identifier, image tag, build number, or commit-derived version.
- `environment`: one of `development`, `staging`, or `production`.
- `status`: one of `pending`, `building`, `deployed`, or `failed`.

The backend validates required fields and rejects unsupported environments or statuses.

### Deployment History

The history view lists deployments newest first. Each row shows:

- creation timestamp
- application name
- version
- environment
- current status
- allowed status actions
- delete action

The frontend fetches history through `GET /api/deployments` and renders the pagination metadata returned by the backend.

### Filters and Pagination

The history endpoint supports server-side filtering:

- `application_name`: partial, case-insensitive search.
- `environment`: exact environment match.
- `status`: exact status match.

Pagination is controlled by:

- `page`: defaults to `1`; values below `1` are normalized to `1`.
- `per_page`: defaults to `10`; minimum is `1`, maximum is `50`.

The API response includes `total`, `pages`, `has_next`, and `has_prev`, which lets the frontend render previous and next controls without guessing.

### Status Lifecycle

Deployments follow a simple state machine:

| Current status | Allowed next statuses |
| --- | --- |
| `pending` | `building`, `failed` |
| `building` | `deployed`, `failed` |
| `deployed` | none |
| `failed` | none |

Updating a deployment to its current status is allowed and treated as a no-op style update. Invalid transitions return `409 Conflict` with a clear error message.

### Duplicate Protection

The database enforces a unique deployment identity across:

- `application_name`
- `version`
- `environment`

This prevents recording the same release of the same application into the same environment more than once. Duplicate creates or updates return `409 Conflict`.

### Health Monitoring

The frontend displays lightweight service health by calling `GET /health`. In local development this reaches Flask through the Vite proxy; in AWS this reaches the web-tier Nginx health endpoint.

The backend also exposes `GET /health/db` for operator diagnostics. That endpoint runs a lightweight `SELECT 1` query and returns `503 Service Unavailable` if the database is unreachable, but it is not used by the normal browser dashboard.

## Tech Stack

### Backend

- Python 3.12
- Flask
- Flask-SQLAlchemy
- Flask-Migrate / Alembic
- Gunicorn for container runtime
- PyMySQL for MySQL support
- python-dotenv for local environment loading

### Frontend

- Vue 3
- Vite
- Plain CSS
- Fetch API for backend communication

### Database

- SQLite for local development and tests
- MySQL 8.4 through Docker Compose

### Testing

- pytest
- Flask test client
- In-memory SQLite test database

## Project Structure

```text
.
|-- backend/
|   |-- api/
|   |   |-- deployments.py      # Deployment CRUD, filtering, pagination, validation
|   |   `-- health.py           # Service and database health checks
|   |-- models/
|   |   `-- deployment.py       # Deployment model and status transition rules
|   |-- __init__.py             # Flask app factory and frontend serving route
|   |-- config.py               # Environment-driven database configuration
|   `-- extensions.py           # SQLAlchemy and Flask-Migrate instances
|-- frontend/
|   |-- src/
|   |   |-- App.vue             # Main dashboard UI and client-side API calls
|   |   |-- main.js             # Vue app entrypoint
|   |   `-- styles.css          # Dashboard styling
|   |-- package.json            # Frontend scripts and dependencies
|   `-- vite.config.js          # Vite dev server and API proxy configuration
|-- migrations/
|   `-- versions/               # Alembic migration history
|-- tests/
|   |-- test_deployments.py     # Deployment API behavior tests
|   `-- test_health.py          # Health endpoint tests
|-- deploy/
|   |-- aws-ec2-three-tier.md   # AWS web/app/RDS deployment guide
|   `-- nginx/                  # Web-tier Nginx template for Vue and /api proxying
|-- Dockerfile                  # Local combined frontend/backend image
|-- docker-compose.yml          # App plus MySQL development stack
|-- backend/Dockerfile          # Cloud app-tier Flask/Gunicorn image
|-- frontend/Dockerfile         # Cloud web-tier Nginx/Vue image
|-- requirements.txt            # Runtime Python dependencies
|-- requirements-dev.txt        # Test/development Python dependencies
`-- wsgi.py                     # Gunicorn/Flask entrypoint
```

## API Reference

### Health Endpoints

#### `GET /health`

Returns a basic service health response.

Example response:

```json
{
  "status": "ok"
}
```

#### `GET /health/db`

Checks database connectivity.

Successful response:

```json
{
  "status": "ok",
  "database": "reachable"
}
```

Failure response:

```json
{
  "status": "error",
  "database": "unreachable",
  "details": "database error details"
}
```

### Deployment Endpoints

#### `GET /api/deployments`

Lists deployments newest first.

Supported query parameters:

| Parameter | Description |
| --- | --- |
| `application_name` | Partial, case-insensitive application name search |
| `environment` | Exact match for `development`, `staging`, or `production` |
| `status` | Exact match for `pending`, `building`, `deployed`, or `failed` |
| `page` | Page number, default `1` |
| `per_page` | Items per page, default `10`, maximum `50` |

Example:

```bash
curl "http://127.0.0.1:5000/api/deployments?environment=staging&status=pending&page=1&per_page=10"
```

Example response:

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
      "created_at": "2026-04-27T14:52:35.659918+00:00",
      "updated_at": "2026-04-27T14:52:35.659918+00:00"
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

#### `GET /api/deployments/<id>`

Returns one deployment by ID.

Example:

```bash
curl http://127.0.0.1:5000/api/deployments/1
```

#### `POST /api/deployments`

Creates a deployment.

Example:

```bash
curl -X POST http://127.0.0.1:5000/api/deployments \
  -H "Content-Type: application/json" \
  -d '{
    "application_name": "billing-api",
    "version": "2026.04.27-1",
    "environment": "staging",
    "status": "pending"
  }'
```

Validation behavior:

- Missing required fields return `400 Bad Request`.
- Invalid `environment` values return `400 Bad Request`.
- Invalid `status` values return `400 Bad Request`.
- Duplicate application/version/environment combinations return `409 Conflict`.

#### `PATCH /api/deployments/<id>`

Updates a deployment. The API currently allows updates to:

- `status`
- `environment`

Example status update:

```bash
curl -X PATCH http://127.0.0.1:5000/api/deployments/1 \
  -H "Content-Type: application/json" \
  -d '{"status": "building"}'
```

Invalid status transitions return `409 Conflict`.

#### `DELETE /api/deployments/<id>`

Deletes a deployment.

Example:

```bash
curl -X DELETE http://127.0.0.1:5000/api/deployments/1
```

Successful response:

```json
{
  "message": "Deployment deleted"
}
```

## Data Model

The `deployments` table contains:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | Integer | Primary key |
| `application_name` | String(120) | Required |
| `version` | String(64) | Required |
| `environment` | String(64) | Required; validated by the API |
| `status` | String(32) | Required; validated by the API |
| `created_at` | DateTime | Automatically set on create |
| `updated_at` | DateTime | Automatically updated on change |

The table also includes a unique constraint named `uq_deployment_app_version_environment`.

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `DEPLOYMENT_NOTES_ENV` | Recommended | Set to `development`, `local`, or `test` to enable the SQLite fallback when no database URL is provided. |
| `DEPLOYMENT_NOTES_DATABASE_URL` | Required outside local/test-style environments unless an SSM database parameter is set | SQLAlchemy database URL. Examples: `sqlite:///instance/deployment_notes.db` or `mysql+pymysql://user:password@host:3306/dbname`. Takes precedence over SSM-based options when set. |
| `DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM` | Recommended on AWS | AWS SSM Parameter Store name containing non-secret DB metadata as JSON: host, port, database, username, and the AWS-managed RDS secret ARN. The app reads the password from Secrets Manager at runtime. |
| `DEPLOYMENT_NOTES_DATABASE_URL_SSM_PARAM` | Legacy optional fallback | AWS SSM Parameter Store name containing the full SQLAlchemy database URL as a SecureString. Prefer `DEPLOYMENT_NOTES_DATABASE_CONFIG_SSM_PARAM` for Terraform-managed AWS deployments. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | Required when using AWS SSM/Secrets Manager | AWS region used by boto3 to read SSM parameters and Secrets Manager secrets. |
| `DEPLOYMENT_NOTES_SERVE_FRONTEND` | Optional | Set to `true` to let Flask serve the compiled Vue app. Defaults to `true` only for `development`, `local`, and `test`; defaults to API-only outside those environments. |
| `FLASK_APP` | Required for Flask CLI commands | Use `wsgi.py`. |
| `PORT` | Optional | Runtime port used by the Docker container. Defaults to `5000`. |
| `APP_INTERNAL_ALB_DNS` | Frontend image only | Internal app ALB DNS name used by the Nginx template to proxy `/api/` from the web tier. |
| `DEPLOYMENT_NOTES_APP_PORT` | Docker Compose only | Host port mapped to the app container. Defaults to `5001`. |
| `DEPLOYMENT_NOTES_MYSQL_DATABASE` | Docker Compose only | MySQL database name. Defaults to `deployment_notes`. |
| `DEPLOYMENT_NOTES_MYSQL_USER` | Docker Compose only | MySQL application user. Defaults to `deployment_notes_user`. |
| `DEPLOYMENT_NOTES_MYSQL_PASSWORD` | Docker Compose only | MySQL application password. Required in `.env`; use a local-only generated value. |
| `DEPLOYMENT_NOTES_MYSQL_ROOT_PASSWORD` | Docker Compose only | MySQL root password. Required in `.env`; use a local-only generated value. |
| `DEPLOYMENT_NOTES_MYSQL_PORT` | Docker Compose only | Host port mapped to MySQL. Defaults to `3307`. |

## Running Locally With SQLite

This is the fastest way to run the project for development or review.

### 1. Create and activate a Python virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install Python dependencies

```bash
pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the runtime dependencies from `requirements.txt` plus pytest.

### 3. Configure Flask and the local database

```bash
export FLASK_APP=wsgi.py
export DEPLOYMENT_NOTES_ENV=development
```

On Windows PowerShell:

```powershell
$env:FLASK_APP = "wsgi.py"
$env:DEPLOYMENT_NOTES_ENV = "development"
```

When `DEPLOYMENT_NOTES_ENV` is `development`, `local`, or `test`, the app falls back to:

```text
sqlite:///instance/deployment_notes.db
```

The application creates the Flask `instance/` directory automatically at startup. If you are running an older checkout or want to create it manually before applying migrations, run:

```bash
mkdir -p instance
```

### 4. Create or upgrade the database schema

```bash
flask db upgrade
```

This applies the Alembic migration that creates the `deployments` table.

### 5. Run the Flask backend

```bash
flask run --host 127.0.0.1 --port 5000
```

The backend will be available at:

```text
http://127.0.0.1:5000
```

If the frontend has not been built yet, the root route may return:

```json
{
  "error": "Frontend build not available"
}
```

The API endpoints will still work.

## Running the Vue Frontend in Development

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server runs on:

```text
http://127.0.0.1:5173
```

The Vite config proxies `/api` and `/health` requests to the Flask backend at `http://127.0.0.1:5000`, so the browser can use the frontend and backend together during development.

## Running as a Production-Style Local Build

Build the frontend:

```bash
cd frontend
npm install
npm run build
cd ..
```

Then run the Flask app:

```bash
export FLASK_APP=wsgi.py
export DEPLOYMENT_NOTES_ENV=development
flask db upgrade
flask run --host 127.0.0.1 --port 5000
```

After `frontend/dist` exists, Flask serves the compiled Vue app from the same origin as the API in local development:

```text
http://127.0.0.1:5000
```

This local combined mode is intentionally different from the AWS three-tier deployment. In AWS, the web tier serves Vue through Nginx and Flask runs API-only on the app tier.

## AWS Three-Tier Deployment

Use the split deployment units for AWS:

```bash
docker build -f frontend/Dockerfile -t deployment-notes-frontend:latest .
docker build -f backend/Dockerfile -t deployment-notes-backend:latest .
```

The target routing model is:

```text
Browser -> Public ALB -> Web EC2/Nginx/Vue -> Internal ALB -> App EC2/Gunicorn/Flask -> RDS MySQL
```

The public ALB should send traffic only to the web tier. The web-tier Nginx config serves static Vue files, exposes an independent `/health`, and proxies `/api/` to the internal app ALB through `APP_INTERNAL_ALB_DNS`.

Production app-tier instances should set:

```dotenv
DEPLOYMENT_NOTES_ENV=production
DEPLOYMENT_NOTES_SERVE_FRONTEND=false
DEPLOYMENT_NOTES_DATABASE_URL=mysql+pymysql://deployment_notes_user:REPLACE_WITH_SECRET_PASSWORD@REPLACE_WITH_RDS_ENDPOINT:3306/deployment_notes
```

Run migrations once per release, not on every instance boot:

```bash
flask --app wsgi.py db upgrade
```

See [deploy/aws-ec2-three-tier.md](deploy/aws-ec2-three-tier.md) for ALB layout, health checks, security groups, RDS notes, migration options, and logging guidance.

## Running With Docker Compose

Docker Compose starts both the combined local Flask/Vue application and a MySQL 8.4 database. This is for local development only; AWS production should use RDS MySQL and the split frontend/backend deployment units.

### 1. Create a local `.env` file

The compose file references `.env` through `env_file`, so create this file before starting the stack. Do not commit `.env` to Git. Start from `.env.example` and replace the password placeholders with local development values:

```dotenv
DEPLOYMENT_NOTES_ENV=development
DEPLOYMENT_NOTES_SERVE_FRONTEND=true
DEPLOYMENT_NOTES_APP_PORT=5001
DEPLOYMENT_NOTES_MYSQL_PORT=3307
DEPLOYMENT_NOTES_MYSQL_DATABASE=deployment_notes
DEPLOYMENT_NOTES_MYSQL_USER=deployment_notes_user
DEPLOYMENT_NOTES_MYSQL_PASSWORD=change-me-use-a-local-dev-password
DEPLOYMENT_NOTES_MYSQL_ROOT_PASSWORD=change-me-use-a-local-root-password
PORT=5000
```

For public repositories, keep only `.env.example` tracked. The real `.env` file should remain local or be managed by your deployment platform's secret manager.

### 2. Start the stack

```bash
docker compose up --build
```

The app will be available at:

```text
http://127.0.0.1:5001
```

MySQL will be available on the host at:

```text
127.0.0.1:3307
```

### 3. Apply database migrations inside the app container

In another terminal:

```bash
docker compose exec deployment-notes-app flask --app wsgi.py db upgrade
```

After the migration runs, the dashboard and API can create and read deployment records.

### 4. Stop the stack

```bash
docker compose down
```

To also delete the MySQL volume:

```bash
docker compose down -v
```

## Running Tests

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the test suite:

```bash
pytest
```

The tests use an in-memory SQLite database, create the schema at test startup, and remove it after each test app fixture is finished.

Current test coverage focuses on:

- service health endpoint
- database health endpoint
- deployment creation
- deployment listing
- filtering
- pagination
- invalid status validation
- status updates
- invalid transition rejection
- deletion

## Example Manual Test Flow

With the backend running on port `5000`, create a deployment:

```bash
curl -X POST http://127.0.0.1:5000/api/deployments \
  -H "Content-Type: application/json" \
  -d '{"application_name":"billing-api","version":"2026.04.27-1","environment":"staging","status":"pending"}'
```

Move it to `building`:

```bash
curl -X PATCH http://127.0.0.1:5000/api/deployments/1 \
  -H "Content-Type: application/json" \
  -d '{"status":"building"}'
```

Move it to `deployed`:

```bash
curl -X PATCH http://127.0.0.1:5000/api/deployments/1 \
  -H "Content-Type: application/json" \
  -d '{"status":"deployed"}'
```

List deployed records in staging:

```bash
curl "http://127.0.0.1:5000/api/deployments?environment=staging&status=deployed"
```

## Design and Engineering Notes

- The backend uses an application factory in `backend/__init__.py`, which keeps test configuration separate from runtime configuration.
- Database setup is environment-driven. Local development can use SQLite without requiring MySQL, while Docker Compose uses MySQL to better resemble a deployed environment.
- Deployment status changes are centralized on the `Deployment` model, making the lifecycle rules easy to inspect and test.
- The API returns structured validation errors instead of failing silently.
- The frontend is intentionally lightweight and communicates with the backend through plain HTTP requests.
- The local combined container uses a multi-stage build: Node builds the Vue assets, then Python/Gunicorn runs the Flask app with the compiled frontend copied into the runtime image.
- The AWS deployment path uses separate frontend and backend images so Flask does not serve Vue static files in the cloud app tier.
- The app is self-contained and independent from the larger platform codebase, which makes it easier to demonstrate as a portfolio project.

## Recruiter-Friendly Summary

This project demonstrates the ability to build a small but complete full-stack application with:

- REST API design
- backend validation
- relational data modeling
- database migrations
- frontend state management
- asynchronous frontend/backend communication
- health checks
- test coverage
- Dockerized deployment
- environment-based configuration

The application is intentionally simple, but it includes the kinds of production-adjacent details that matter in real internal tools: predictable API responses, validation, duplicate protection, database health checks, migration support, test isolation, and a containerized runtime path.
