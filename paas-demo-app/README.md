# PaaS Control Plane

This repository is the PaaS platform backend. It manages projects, builds, deployments, deployment events, and the worker that advances deployments through the deployment state machine.

## Repository Layout

```text
paas-demo-app/
├── backend/
├── frontend/
├── worker/
├── migrations/
├── tests/
├── docs/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── wsgi.py
```

Additional reference:

- [Security Notes](docs/security.md)
- [Operations Runbook](docs/runbook.md)

## Local Quality Commands

Install runtime and CI/dev dependencies:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

Run the full backend test suite:

```bash
.venv/bin/python -m pytest
```

Run lint checks:

```bash
.venv/bin/ruff check backend worker wsgi.py
```

Run the operator frontend locally:

```bash
cd frontend
npm ci
npm run dev
```

Build and test the frontend:

```bash
cd frontend
npm run test
npm run build
```

## Operator Frontend

The control plane includes a Vue/Vite operator console served by Flask in built images and by Vite during local development.

Current UI scope:

- bearer token storage for protected APIs
- platform health and activity
- project create, edit, and delete
- project environment variable management for literal values, ConfigMap key refs, and Secret key refs
- default test command configuration
- deploy/test trigger, retry, redeploy, and stop actions
- deployment summary, events, build logs, runtime logs, and Kubernetes diagnostics

Run security checks:

```bash
.venv/bin/bandit -c bandit.yaml -r backend worker wsgi.py
.venv/bin/pip-audit -r requirements.txt
```

Validate that migrations apply cleanly:

```bash
mkdir -p instance
rm -f instance/ci-control-plane.db
CONTROL_PLANE_ENV=development \
CONTROL_PLANE_DATABASE_URL="sqlite:////$(pwd)/instance/ci-control-plane.db" \
.venv/bin/python -m flask --app wsgi:app db upgrade
```

Validate the control-plane container build:

```bash
docker build --tag paas-control-plane:local .
```

Validate the deployment assets:

```bash
docker compose config
helm template ci ./deploy/helm/paas-control-plane -f ./deploy/helm/paas-control-plane/values.ci.yaml > /dev/null
helm template local ./deploy/helm/paas-control-plane -f ./deploy/helm/paas-control-plane/values.local-microk8s.yaml > /dev/null
helm lint ./deploy/helm/generic-web-app
helm template generic ./deploy/helm/generic-web-app > /dev/null
helm template generic-minimal ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/minimal.yaml > /dev/null
helm template generic-node ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/node-express.yaml > /dev/null
helm template generic-python ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/python-fastapi.yaml > /dev/null
```

## Scope

- `GET /health`
- `GET /health/db`
- `GET /health/platform`
- `GET /health/activity`
- `GET /api/audit-events`
- `GET/POST/PATCH/DELETE /api/projects`
- `GET /api/projects/<id>/activity`
- `GET /api/projects/<id>/builds`
- `GET /api/projects/<id>/deployments`
- `GET/POST/PATCH /api/projects/<id>/deployments`
- `GET /api/projects/<id>/deployments/<deployment_id>/summary`
- `GET /api/projects/<id>/deployments/<deployment_id>/kubernetes-diagnostics`
- `GET /api/projects/<id>/deployments/<deployment_id>/events`
- `GET /api/projects/<id>/deployments/<deployment_id>/runtime-log`
- `POST /api/webhooks/github`
- `python -m flask --app wsgi:app run-worker-once`
- `python -m flask --app wsgi:app run-worker`
- `python -m flask --app wsgi:app run-reconciler`
- `python -m flask --app wsgi:app run-reconciler-loop`

## Deployment Story

The repository now includes two operator-facing deployment paths:

- local Docker Compose for API, worker, reconciler, migrations, and MySQL
- an internal Helm chart for production-like Kubernetes deployment of the control plane
- a stack-agnostic generic web app Helm chart that documents the intended user workload abstraction

Local startup, Kubernetes rollout, migration flow, required config/secrets, health endpoints, and troubleshooting are documented in [docs/runbook.md](docs/runbook.md).

## Database

The control plane uses its own database configuration:

- `CONTROL_PLANE_DATABASE_URL`
- `CONTROL_PLANE_ENV`

For local development, `CONTROL_PLANE_ENV=development` falls back to `sqlite:///instance/control_plane.db`.

`CONTROL_PLANE_ENV` also controls whether project `repo_url` may point at a local filesystem repository:

- `development`: local repository paths are allowed
- non-development environments: project `repo_url` must be a supported remote Git repository URL

Local repository paths are intended only for:

- local integration testing
- local development workflows

For production-style remote repositories, the control plane currently supports GitHub HTTPS URLs only. Accepted forms are:

- `https://github.com/<owner>/<repo>`
- `https://github.com/<owner>/<repo>.git`

Those values are normalized and stored internally as:

- `https://github.com/<owner>/<repo>.git`

Private repositories are supported through GitHub HTTPS plus token auth. SSH Git URLs and non-GitHub remote URLs are not supported yet.

## API Authentication

The control-plane API supports bearer-token authentication with a small role model intended for operator and portfolio use rather than end-user identity management.

Configure any of these environment variables to enable API authentication:

- `CONTROL_PLANE_API_TOKEN_READ_ONLY`
- `CONTROL_PLANE_API_TOKEN_DEPLOYER`
- `CONTROL_PLANE_API_TOKEN_ADMIN`

When no API token variables are configured, bearer-token auth is disabled. This is intended only for local development and test workflows.

Use the token as:

```bash
Authorization: Bearer <token>
```

Role matrix:

- `read_only`: read/list/show routes, logs, summaries, diagnostics, `/health/db`, `/health/platform`, and `/health/activity`
- `deployer`: everything in `read_only` plus deploy, retry, and redeploy actions
- `admin`: everything in `deployer` plus project create/update/delete, manual deployment record creation, and deployment patch/stop operations

Public routes:

- `GET /health`

Webhook security remains separate:

- `POST /api/webhooks/github` uses GitHub signature verification
- webhook requests do not use bearer API tokens

Authentication responses:

- missing bearer token: `401`
- invalid bearer token: `401`
- valid bearer token without sufficient role: `403`

Security notes and tradeoffs:

- tokens are configured through environment variables rather than a user database or OAuth flow
- the control plane compares bearer tokens with constant-time comparison
- route logic does not embed raw token values
- the application does not log raw bearer tokens
- this is intentionally a small operator-facing security boundary, not a commercial identity system

## Audit Trail

The control plane persists a small audit trail for important mutating API actions. This is intended to demonstrate security awareness and operational visibility rather than full compliance-grade auditing.

Currently audited actions include:

- project created
- project updated
- project deleted
- deploy triggered
- retry triggered
- redeploy triggered
- manual deployment created
- deployment patched
- deployment stopped
- denied mutating API attempts on protected project routes

Audit records include:

- timestamp
- action
- actor role
- resource type and id
- success or failure status
- request id when provided through `X-Request-Id`
- client IP address when available
- a small redacted metadata object

Audit endpoint:

- `GET /api/audit-events`

This endpoint requires at least `read_only` access when API auth is enabled and supports:

- `limit`
- `before_id`

Audit tradeoffs:

- the control plane never stores raw bearer tokens
- audit metadata is intentionally small and redacted
- secrets, environment variable values, registry credentials, and webhook payload bodies are not persisted in audit records
- webhook authentication remains separate and webhook payloads are not copied into the audit trail

## Request Correlation

The control plane supports lightweight request correlation through `X-Request-ID`.

Behavior:

- if the client sends a valid `X-Request-ID`, the control plane reuses it
- if the header is missing, too long, or invalid, the control plane generates a safe request id
- every HTTP response includes `X-Request-ID`
- audit events persist the request id
- request completion logs include the request id

This is intentionally lightweight request correlation for operator workflows. It is not distributed tracing and does not introduce spans, trace propagation, or tracing infrastructure.

## Secret Handling

The control plane now distinguishes normal project configuration from secret-bearing configuration in `env_vars`.

Use these shapes:

- normal literal config:
  - `{"name":"LOG_LEVEL","value":"info"}`
- secret literal config for local-style executors:
  - `{"name":"DATABASE_URL","value":"postgres://...","is_secret":true}`
- Kubernetes ConfigMap reference:
  - `{"name":"APP_ENV","value_source":"configmap_key_ref","source_name":"my-app-config","source_key":"app-env"}`
- Kubernetes Secret reference:
  - `{"name":"DATABASE_URL","value_source":"secret_key_ref","source_name":"my-app-secret","source_key":"database-url"}`

Current behavior:

- normal config values remain readable in project responses
- secret literal values are stored for execution but redacted from API responses as `[REDACTED]`
- Kubernetes `secret_key_ref` entries are treated as secret config by definition
- when `CONTROL_PLANE_EXECUTOR=kubernetes`, secret env vars must use `secret_key_ref` instead of literal values

Redaction coverage:

- project list/show/create/update responses
- deployment read models and event payloads
- deployment summaries and Kubernetes diagnostics
- build and runtime log API responses
- audit metadata
- request/operation error payloads that include deployment metadata

Executor behavior:

- `local-docker` receives literal secret values at runtime, but the worker redacts them from command metadata, deploy logs, runtime-log capture, and healthcheck summaries
- `kubernetes` uses existing Secret references and does not render literal secret values into generated manifests

Current storage tradeoff:

- secret literal values are still stored in the project record when you use `is_secret: true`
- they are masked on read rather than encrypted at rest because this project does not yet have key-management infrastructure
- for a stronger production-oriented posture, move secret delivery to Kubernetes Secrets, Vault, or another external secret manager instead of database-backed literal secrets

## Quality Gates / CI

GitHub Actions now runs a small but meaningful CI suite on pushes to `main` and on pull requests.

Current CI checks:

- `Tests`: runs the Python test suite, including auth, audit, request-correlation, Kubernetes, worker, and secret-redaction coverage
- `Ruff`: runs lightweight Python linting with a small baseline focused on real code correctness issues
- `Bandit`: runs a targeted security scan against `backend/`, `worker/`, and `wsgi.py`
- `Migrations`: applies Alembic/Flask-Migrate migrations against a fresh SQLite database
- `Docker Build`: verifies that the control-plane Docker image still builds
- `Dependency Audit`: runs `pip-audit` as an informational check

Why these gates matter:

- tests protect behavior and deployment orchestration confidence
- lint catches correctness issues early without turning this repo into a formatting exercise
- security scanning reinforces the portfolio story around safe defaults and review discipline
- migration checks make schema changes more credible
- Docker build validation proves the application is still packageable and runnable

Current tradeoffs:

- `pip-audit` is non-blocking because advisory data changes outside the repo and the dependency set is still relatively lightweight
- `bandit` skips a small set of subprocess and workspace-path heuristics because this control plane intentionally orchestrates external tools such as `git`, `docker`, and `kubectl`, and uses an explicit workspace root under operator control
- mypy is not enforced yet because the codebase does not have a clean typing baseline, and adding it now would create more noise than signal

Future hardening options:

- add a typed baseline and introduce mypy incrementally
- pin direct dependencies more tightly to make dependency-audit results more stable
- split fast PR checks from slower scheduled security checks if CI time grows
- add image scanning or SBOM generation when the project is ready for a stronger supply-chain story

For a project-scoped operator view, `GET /api/projects/<id>/activity` reports:

- latest deployments for that project
- current active deployment, if any
- latest failed deployment with last error
- recent webhook deliveries relevant to that project
- compact summary fields such as latest deployment time and recent counts

This endpoint supports:

- `latest_limit`
- `webhook_limit`
- `deployment_status`
- `webhook_status`
- `active_only`
- `include_latest_failed`
- `include_webhooks`
- `before_deployment_id`
- `before_webhook_delivery_id`

Each limit must be an integer between `1` and `100`.

The boolean flags accept standard truthy/falsy forms such as `true` / `false`.

For project deployment history, `GET /api/projects/<id>/deployments` now supports:

- `limit`
- `deployment_status`
- `before_deployment_id`

and returns an `items` array plus a `pagination` block with the next cursor hint.

For a compact project readiness summary, `GET /api/projects/<id>/status` reports:

- latest deployment summary for the project
- active deployment summary, if any
- latest failed deployment summary, if any
- whether deployment creation is currently ready under the active executor config
- the current deploy-readiness error when it is not ready
- compact counts and latest deployment timestamp

For a quick operator-facing config summary, `GET /health/platform` reports:

- active `CONTROL_PLANE_ENV`
- active `CONTROL_PLANE_EXECUTOR`
- API auth posture for bearer-token protection
- whether local repository paths are currently allowed
- whether deployment creation is currently ready for the selected executor
- registry config readiness at the config level
- Kubernetes deployment prereq readiness at the config level

This endpoint is intentionally config-only. It does not perform live Docker, registry, database, or Kubernetes API checks beyond the separate `/health/db` probe.

For a quick operator-facing activity view, `GET /health/activity` reports:

- latest deployments across projects
- currently pending or running deployments
- recent failed deployments with last error
- recent ignored webhook deliveries with reason
- recent accepted webhook deliveries
- compact summary fields such as latest deployment time and recent counts

This endpoint is read-only and is built from persisted project/build/deployment records rather than worker logs or raw event streams.

It also supports optional per-section limits:

- `latest_limit`
- `active_limit`
- `failed_limit`
- `ignored_webhook_limit`
- `accepted_webhook_limit`
- `before_deployment_id`
- `before_webhook_delivery_id`

Each limit must be an integer between `1` and `100`.

It also supports optional status filters:

- `deployment_status`
  Values must come from the deployment state machine, for example `pending`, `running`, `failed`.
- `webhook_status`
  Supported values are `accepted` and `ignored`.
- `project_id`
  Narrows the platform-wide activity view to one project id.

Both activity endpoints also return compact pagination metadata including the next cursor hints.

## Worker Execution

The worker supports two executor modes:

- `fake`: default, test-friendly executor with simulated infrastructure behavior
- `local-docker`: clones a Git repository into a local workspace, builds a Docker image, optionally runs tests in the built image, optionally tags and pushes to a registry, starts a local container, and waits for the configured healthcheck to succeed
- `kubernetes`: clones a Git repository into a local workspace, builds a Docker image, optionally runs tests, tags and pushes the image to a registry, applies a minimal Kubernetes Deployment and Service, waits for rollout, and then healthchecks the app through a temporary `kubectl port-forward`

Useful settings:

- `CONTROL_PLANE_EXECUTOR`
- `CONTROL_PLANE_WORKSPACE_ROOT`
- `CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS`
- `CONTROL_PLANE_COMMAND_RETRY_COUNT`
- `CONTROL_PLANE_REGISTRY_ENABLED`
- `CONTROL_PLANE_REGISTRY_URL`
- `CONTROL_PLANE_REGISTRY_NAMESPACE`
- `CONTROL_PLANE_REGISTRY_USERNAME`
- `CONTROL_PLANE_REGISTRY_PASSWORD`
- `CONTROL_PLANE_API_TOKEN_READ_ONLY`
- `CONTROL_PLANE_API_TOKEN_DEPLOYER`
- `CONTROL_PLANE_API_TOKEN_ADMIN`
- `CONTROL_PLANE_GITHUB_WEBHOOK_SECRET`
- `CONTROL_PLANE_KUBECONFIG`
- `CONTROL_PLANE_K8S_NAMESPACE`
- `CONTROL_PLANE_K8S_IMAGE_PULL_SECRET`
- `CONTROL_PLANE_DEPLOY_HOST`
- `CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS`
- `CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS`
- `CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS`
- `CONTROL_PLANE_WORKER_ID`
- `CONTROL_PLANE_CLAIM_TTL_SECONDS`
- `CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS`

Pending deployments are now claimed with a worker lease before processing. That prevents two worker processes from picking the same row concurrently, and stale claims can be reclaimed after the configured TTL.
For long-running executor commands, the worker now refreshes claims through a heartbeat callback so active deployments do not become reclaimable mid-build or mid-test.

When registry support is enabled, the build and publish flow is:

- clone
- build local image
- test local image
- tag local image for the registry
- push registry image

The final deploy step depends on the executor:

- `local-docker`: deploy locally from the local image
- `kubernetes`: deploy the pushed `image_ref` into Kubernetes

The Kubernetes executor is intentionally narrow in this iteration:

- MicroK8s-compatible kubeconfig access
- minimal Deployment + Service resources only
- rollout wait via `kubectl rollout status`
- healthcheck via temporary `kubectl port-forward`
- minimal stop support through resource deletion
- optional env var injection from existing ConfigMaps and Secrets

Not included yet:

- Helm
- ingress
- cert-manager
- autoscaling
- namespaces per app
- secrets abstraction
- GitOps
- multi-cluster support

## GitHub Webhooks

GitHub push-triggered deploys are supported through:

- `POST /api/webhooks/github`

This endpoint is intentionally lightweight. It verifies the webhook signature, filters for supported events, and then creates deployments through the existing control-plane deployment flow. It does not introduce a queue, GitHub App authentication, or a separate deployment creation path.

### Setup

1. Configure the webhook secret in the control plane environment:

```bash
export CONTROL_PLANE_GITHUB_WEBHOOK_SECRET='replace-with-a-random-secret'
```

2. Create or update a project with:

- `trigger` set to `github_push`
- `repo_url` set to the GitHub repository URL
- `branch` set to the branch that should auto-deploy

3. In the GitHub repository webhook settings:

- set the payload URL to `https://<control-plane-host>/api/webhooks/github`
- set content type to `application/json`
- set the same secret value
- subscribe to the `push` event

### Behavior

Current webhook behavior is:

- `ping` events are accepted and return `200`
- `push` events are supported
- unsupported GitHub events are ignored with `202`
- push events for unmatched repositories are ignored with `202`
- push events for non-configured branches are ignored with `202`
- duplicate `X-GitHub-Delivery` values are ignored with `202`
- the same commit SHA may still create multiple deployments if GitHub sends different delivery IDs

Projects are matched only when all of the following are true:

- `project.trigger == "github_push"`
- the normalized repository URL matches the webhook repository
- `project.branch` matches the pushed branch exactly

Webhook-triggered deploys reuse the normal deployment-record flow. When a project defines `default_test_command`, webhook-created deployments inherit it; otherwise they proceed without a test command.

### Delivery Semantics

The control plane persists webhook delivery records for replay protection and auditability. Each delivery tracks:

- `delivery_id`
- `event_type`
- `repository_url`
- `branch`
- `commit_sha`
- `status`
- `reason`
- `deployment_id` when exactly one deployment was created
- `received_at`

Current delivery statuses are:

- `accepted`
- `ignored`

Current ignore reasons include:

- `unsupported_event_type`
- `unsupported_ref`
- `unmatched_repository`
- `branch_mismatch`
- `duplicate_delivery`

Webhook-triggered deployments also record a `webhook.github_push_received` deployment event with branch, commit SHA, repository URL, and GitHub delivery ID metadata.

## Reconciliation

The control plane also provides a manual reconciliation command:

```bash
.venv/bin/flask --app wsgi:app run-reconciler
```

The reconciler is independent from the main worker loop and safely repairs inconsistent state. In this iteration it:

- clears stale deployment claims
- marks `running` `local-docker` deployments as failed when their container is missing
- removes orphan containers from failed deployments
- removes stale workspace/log artifacts from failed deployments

All reconciliation actions are best-effort and recorded as deployment events such as:

- `reconcile.claim_cleared`
- `reconcile.claim_recovered`
- `reconcile.container_missing`
- `reconcile.container_removed`
- `reconcile.workspace_removed`
- `reconcile.cleanup_failed`

## Local Docker Manual Flow

This repo no longer contains a sample app, so the simplest end-to-end worker test is a tiny local Git repository.

1. Create a local test repository:

```bash
mkdir -p /tmp/local-docker-app
cd /tmp/local-docker-app
git init -b main
cat > Dockerfile <<'EOF'
FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 5000
CMD ["python", "/app/server.py"]
EOF
cat > server.py <<'EOF'
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return


HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
EOF
git add Dockerfile
git add server.py
git commit -m "init local docker app"
```

2. Start the control plane with the local Docker executor:

```bash
cd /home/adela/autodeploy-platform/paas-demo-app
unset CONTROL_PLANE_DATABASE_URL DATABASE_URL APP_ENV
export CONTROL_PLANE_ENV=development
export CONTROL_PLANE_EXECUTOR=local-docker
.venv/bin/flask --app wsgi:app db upgrade
.venv/bin/flask --app wsgi:app run --debug
```

3. Create a project that points at the local Git repo:

```bash
curl -X POST http://127.0.0.1:5000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "local-docker-app",
    "repo_url": "/tmp/local-docker-app",
    "branch": "main",
    "dockerfile_path": "Dockerfile",
    "build_context": ".",
    "port": 5000,
    "healthcheck_path": "/health",
    "env_vars": [],
    "trigger": "manual",
    "runtime": "dockerfile"
  }'
```

Local filesystem repository paths like `/tmp/local-docker-app` are accepted only when `CONTROL_PLANE_ENV=development`. In production-like environments, project creation and update requests must use supported remote Git repository URLs instead.

4. Create a pending deployment:

```bash
curl -X POST http://127.0.0.1:5000/api/projects/1/deployments \
  -H 'Content-Type: application/json' \
  -d '{
    "commit_sha": "localmain001",
    "image_name": "local-docker-app",
    "image_tag": "localmain001",
    "status": "pending",
    "build_status": "pending",
    "test_command": "sh -c true"
  }'
```

5. Run the worker:

```bash
.venv/bin/flask --app wsgi:app run-worker-once
```

6. Inspect the deployment and events:

```bash
curl http://127.0.0.1:5000/api/projects/1/deployments
curl http://127.0.0.1:5000/api/projects/1/deployments/1/events
curl http://127.0.0.1:5000/api/projects/1/deployments/1/runtime-log
```

The deployment should end in `running`, with event metadata that includes command summaries, output tails, attempt counts, log paths, workspace paths, and deploy target information.

In `local-docker` mode, the deployment apply event also includes:

- `container_name`
- `container_id`
- `host_port`
- `published_port`
- `healthcheck_url`
- `healthcheck_status_code`
- `healthcheck_attempts`
- `runtime_log_path`
- `runtime_log_summary`
- `runtime_output_tail`

7. Stop the local deployment and clean up the container:

```bash
curl -X PATCH http://127.0.0.1:5000/api/projects/1/deployments/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "status": "stopped",
    "message": "Stop requested"
  }'
```

That stop request uses the deployment executor for the recorded `deploy_target`. In `local-docker` mode it captures container logs, removes the running container, clears `service_url`, and records `deployment.stop_started` and `deployment.stopped` events with cleanup metadata.

The runtime log endpoint returns tailed container output as JSON. By default it returns the last 200 lines, and you can override that with `?tail_lines=<n>` up to 2000.

## Kubernetes Manual Flow

The Kubernetes executor targets a local MicroK8s-style cluster through a kubeconfig file and a registry-pushed image.

### Assumptions

- MicroK8s is running locally
- `kubectl` can reach the cluster using the configured kubeconfig
- the cluster can pull the pushed image from Docker Hub or another reachable registry
- the application exposes a single HTTP port and a working healthcheck path

### Required Settings

```bash
export CONTROL_PLANE_ENV=development
export CONTROL_PLANE_EXECUTOR=kubernetes
export CONTROL_PLANE_REGISTRY_ENABLED=true
export CONTROL_PLANE_REGISTRY_URL=docker.io
export CONTROL_PLANE_REGISTRY_NAMESPACE=<your-dockerhub-namespace>
export CONTROL_PLANE_REGISTRY_USERNAME=<your-dockerhub-username>
export CONTROL_PLANE_REGISTRY_PASSWORD=<your-dockerhub-password-or-token>
export CONTROL_PLANE_KUBECONFIG=/path/to/microk8s-config
export CONTROL_PLANE_K8S_NAMESPACE=default
export CONTROL_PLANE_K8S_IMAGE_PULL_SECRET=<existing-kubernetes-secret-name>
```

`CONTROL_PLANE_EXECUTOR=kubernetes` requires registry push to succeed before deploy. The Kubernetes executor deploys using `build.image_ref`, not the local image tag.

The control plane now rejects deployment creation up front when Kubernetes mode is selected but the required executor settings are incomplete. At minimum, deployment-creation paths require:

- `CONTROL_PLANE_REGISTRY_ENABLED=true`
- `CONTROL_PLANE_REGISTRY_URL`
- `CONTROL_PLANE_REGISTRY_NAMESPACE`
- `CONTROL_PLANE_KUBECONFIG`

If `CONTROL_PLANE_K8S_IMAGE_PULL_SECRET` is set, the Kubernetes executor adds that existing secret name under `imagePullSecrets` in the generated Pod spec. This is a reference only: the control plane does not create or manage the secret in this iteration.

Before `kubectl apply`, the Kubernetes executor now performs a read-only preflight check for:

- referenced `ConfigMap`s from project `env_vars`
- referenced `Secret`s from project `env_vars`
- `CONTROL_PLANE_K8S_IMAGE_PULL_SECRET` when configured

If any of those resources are missing, the deploy fails early with `kubernetes.preflight_failed` instead of proceeding into apply, rollout, or healthcheck.

### Start the Control Plane

```bash
cd /home/adela/autodeploy-platform/paas-demo-app
unset CONTROL_PLANE_DATABASE_URL DATABASE_URL APP_ENV
.venv/bin/flask --app wsgi:app db upgrade
.venv/bin/flask --app wsgi:app run --debug
```

### Create a Project

Use a repository that contains a valid Dockerfile and exposes the configured HTTP port:

```bash
curl -X POST http://127.0.0.1:5000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "microk8s-app",
    "repo_url": "https://github.com/example/microk8s-app",
    "branch": "main",
    "dockerfile_path": "Dockerfile",
    "build_context": ".",
    "port": 5000,
    "healthcheck_path": "/health",
    "env_vars": [],
    "trigger": "manual",
    "runtime": "dockerfile"
  }'
```

If a Kubernetes deployment needs environment variables from existing cluster resources, use the project `env_vars` field with one of these shapes:

- literal value:
  - `{"name":"LOG_LEVEL","value":"info"}`
- secret literal value for local executors:
  - `{"name":"DATABASE_URL","value":"postgres://...","is_secret":true}`
- ConfigMap key reference:
  - `{"name":"APP_ENV","value_source":"configmap_key_ref","source_name":"my-app-config","source_key":"app-env"}`
- Secret key reference:
  - `{"name":"DATABASE_URL","value_source":"secret_key_ref","source_name":"my-app-secret","source_key":"database-url"}`

When `CONTROL_PLANE_EXECUTOR=kubernetes`, project create and update validation also enforces the Kubernetes-compatible shape of these definitions:

- env var `name` must be a valid Kubernetes environment variable name
- referenced `source_name` values must be valid Kubernetes resource names
- referenced `source_key` values must be non-empty
- duplicate env var names are rejected
- secret env vars cannot use literal values; use `secret_key_ref`

These checks are schema-only. They do not contact the cluster or verify that the referenced `ConfigMap` or `Secret` actually exists.

In this iteration, the control plane:

- does not create ConfigMaps
- does not create Secrets
- may store secret literal values for local-style execution, but masks them on read
- assumes referenced Kubernetes resources already exist

### Trigger a Deployment

The user-facing deploy endpoint is the simplest path because it resolves the current commit SHA and creates the pending build/deployment records for you:

```bash
curl -X POST http://127.0.0.1:5000/api/projects/1/deploy \
  -H 'Content-Type: application/json' \
  -d '{}'
```

### Run the Worker

```bash
.venv/bin/flask --app wsgi:app run-worker-once
```

### Inspect the Result

```bash
curl http://127.0.0.1:5000/api/projects/1/deployments/latest
curl http://127.0.0.1:5000/api/projects/1/deployments/1/events
curl http://127.0.0.1:5000/api/projects/1/deployments/1/summary
curl http://127.0.0.1:5000/api/projects/1/deployments/1/kubernetes-diagnostics
```

For a successful Kubernetes deployment, the event stream should include:

- `image.push_succeeded`
- `deployment.apply_started`
- `kubernetes.preflight_started`
- `kubernetes.preflight_succeeded`
- `kubernetes.manifest_apply_started`
- `kubernetes.manifest_apply_succeeded`
- `kubernetes.rollout_started`
- `kubernetes.rollout_succeeded`
- `kubernetes.healthcheck_started`
- `kubernetes.healthcheck_succeeded`
- `deployment.apply_succeeded`
- `deployment.running`

The deployment summary and apply event metadata include Kubernetes-specific fields such as:

- `deploy_target: kubernetes`
- `deployment_name`
- `service_name`
- `namespace`
- `manifest_path`
- `service_url`
- `healthcheck_url`
- `port_forward_local_port`

The deployment summary endpoint also exposes Kubernetes-specific read-model fields:

- `kubernetes_namespace`
- `kubernetes_deployment_name`
- `kubernetes_service_name`
- `last_kubernetes_failure_stage`
- `last_kubernetes_failure_summary`
- `last_kubernetes_failure_missing_resources`

For healthy Kubernetes deployments, those fields identify the active namespace and generated resource names.

For failed Kubernetes deployments, `last_kubernetes_failure_stage` is one of:

- `preflight`
- `manifest_apply`
- `rollout`
- `healthcheck`

`last_kubernetes_failure_summary` is a concise extracted diagnostic summary from the most recent Kubernetes failure event so clients do not need to parse the raw event stream just to display the main failure cause.

For preflight failures, `last_kubernetes_failure_missing_resources` contains the missing `ConfigMap` and `Secret` references directly from the preflight event metadata.

If a client needs the latest Kubernetes failure context in a more stable read model than raw events, `GET /api/projects/<id>/deployments/<deployment_id>/kubernetes-diagnostics` returns:

- `failure_stage`
- `failure_summary`
- `missing_resources`
- `checked_resources`
- `pod_names`
- `pod_describe_summary`
- `pod_logs_summary`
- `diagnostics`

The `diagnostics` field contains the raw metadata from the latest Kubernetes failure event, while the top-level fields extract the most relevant parts for direct display.

### Healthcheck Behavior

The Kubernetes executor does not expose an ingress or NodePort in this iteration. After rollout succeeds, it performs the healthcheck by:

1. starting a temporary `kubectl port-forward` to the generated Service
2. probing the configured `healthcheck_path`
3. marking the deployment `running` only after the healthcheck succeeds

The temporary port-forward process is cleaned up automatically after the probe completes.

### Failure Diagnostics

The Kubernetes executor now captures extra read-only diagnostics on common failure paths.

For `kubernetes.preflight_failed`, the control plane records which referenced resources were checked and which were missing before any manifest apply happens.

The failure metadata includes:

- `preflight_log_path`
- `checked_resources`
- `missing_resources`
- `missing_resource_names`
- `missing_resource_types`
- `configmap_refs_used`
- `secret_refs_used`
- `image_pull_secret`

For `kubernetes.manifest_apply_failed`, the control plane also captures:

- `kubectl get pods -o wide`
- `kubectl get services`
- `kubectl get pods -o name`
- `kubectl describe pod/<pod_name>`
- `kubectl logs pod/<pod_name> --tail 50`

The failure metadata includes:

- `apply_pods_log_path`
- `apply_pods_summary`
- `apply_pods_output_tail`
- `apply_services_log_path`
- `apply_services_summary`
- `apply_services_output_tail`
- `apply_pod_names`
- `apply_pod_describe_log_paths`
- `apply_pod_describe_summary`
- `apply_pod_logs_log_paths`
- `apply_pod_logs_summary`

For `kubernetes.rollout_failed`, the control plane also captures:

- `kubectl get pods -o wide`
- `kubectl describe deployment/<deployment_name>`
- `kubectl get pods -l app.kubernetes.io/instance=<deployment_name> -o name`
- `kubectl describe pod/<pod_name>`
- `kubectl logs pod/<pod_name> --tail 50`

The failure metadata includes:

- `rollout_pods_log_path`
- `rollout_pods_summary`
- `rollout_pods_output_tail`
- `rollout_describe_log_path`
- `rollout_describe_summary`
- `rollout_describe_output_tail`
- `rollout_pod_names`
- `rollout_pod_describe_log_paths`
- `rollout_pod_describe_summary`
- `rollout_pod_logs_log_paths`
- `rollout_pod_logs_summary`

For `kubernetes.healthcheck_failed`, the control plane also captures:

- `kubectl get pods -o wide`
- `kubectl describe deployment/<deployment_name>`
- `kubectl describe service/<service_name>`
- `kubectl get pods -l app.kubernetes.io/instance=<deployment_name> -o name`
- `kubectl describe pod/<pod_name>`
- `kubectl logs pod/<pod_name> --tail 50`

The failure metadata includes:

- `healthcheck_pods_log_path`
- `healthcheck_pods_summary`
- `healthcheck_pods_output_tail`
- `healthcheck_deployment_log_path`
- `healthcheck_deployment_summary`
- `healthcheck_deployment_output_tail`
- `healthcheck_service_log_path`
- `healthcheck_service_summary`
- `healthcheck_service_output_tail`
- `healthcheck_pod_names`
- `healthcheck_pod_describe_log_paths`
- `healthcheck_pod_describe_summary`
- `healthcheck_pod_logs_log_paths`
- `healthcheck_pod_logs_summary`

These diagnostics are best-effort. They are intended to make common issues such as image pull failures, failed scheduling, or manifest admission problems visible through deployment events without requiring direct cluster access during the first debugging pass.

When the reconciler detects Kubernetes drift, it also records best-effort pod diagnostics in reconcile events such as `reconcile.kubernetes_missing_resource` and `reconcile.kubernetes_resources_removed`. That metadata uses `reconcile_*` or `reconcile_cleanup_*` field prefixes and is intended to preserve the last useful pod context even when the deployment is no longer healthy.

### Stop a Kubernetes Deployment

```bash
curl -X PATCH http://127.0.0.1:5000/api/projects/1/deployments/1 \
  -H 'Content-Type: application/json' \
  -d '{
    "status": "stopped",
    "message": "Stop requested"
  }'
```

In Kubernetes mode, stop performs a minimal cleanup by deleting the generated Deployment and Service resources and recording Kubernetes delete events before the final `deployment.stopped` event.
