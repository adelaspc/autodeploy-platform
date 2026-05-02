# PaaS Control Plane

This repository is the PaaS platform backend. It manages projects, builds, deployments, deployment events, and the worker that advances deployments through the deployment state machine.

## Repository Layout

```text
paas-demo-app/
├── backend/
├── worker/
├── migrations/
├── tests/
├── docs/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── wsgi.py
```

## Scope

- `GET/POST/PATCH/DELETE /api/projects`
- `GET /api/projects/<id>/builds`
- `GET/POST/PATCH /api/projects/<id>/deployments`
- `GET /api/projects/<id>/deployments/<deployment_id>/events`
- `GET /api/projects/<id>/deployments/<deployment_id>/runtime-log`
- `python -m flask --app wsgi:app run-worker-once`
- `python -m flask --app wsgi:app run-worker`
- `python -m flask --app wsgi:app run-reconciler`

## Database

The control plane uses its own database configuration:

- `CONTROL_PLANE_DATABASE_URL`
- `CONTROL_PLANE_ENV`

For local development, `CONTROL_PLANE_ENV=development` falls back to `sqlite:///instance/control_plane.db`.

## Worker Execution

The worker supports two executor modes:

- `fake`: default, test-friendly executor with simulated infrastructure behavior
- `local-docker`: clones a Git repository into a local workspace, builds a Docker image, optionally runs tests in the built image, optionally tags and pushes to a registry, starts a local container, and waits for the configured healthcheck to succeed

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
- `CONTROL_PLANE_DEPLOY_HOST`
- `CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS`
- `CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS`
- `CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS`
- `CONTROL_PLANE_WORKER_ID`
- `CONTROL_PLANE_CLAIM_TTL_SECONDS`
- `CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS`

Pending deployments are now claimed with a worker lease before processing. That prevents two worker processes from picking the same row concurrently, and stale claims can be reclaimed after the configured TTL.
For long-running executor commands, the worker now refreshes claims through a heartbeat callback so active deployments do not become reclaimable mid-build or mid-test.

When registry support is enabled, the deployment flow is:

- clone
- build local image
- test local image
- tag local image for the registry
- push registry image
- deploy locally from the local image

The control plane still deploys locally after push in this iteration. Kubernetes is not part of the current flow yet.

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
