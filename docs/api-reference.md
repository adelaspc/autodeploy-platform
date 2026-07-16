# API Reference

The JSON API is rooted at `/api`; health and metrics endpoints are rooted at `/`. This page is the canonical route and query-parameter index. Domain lifecycle semantics are documented in [Deployment Model](deployment.md), and authentication details in [Security](security.md).

## Conventions

- Send JSON request bodies with `Content-Type: application/json`.
- When API authentication is configured, send `Authorization: Bearer <token>`.
- Every response includes `X-Request-ID`. A valid client-supplied `X-Request-ID` is reused; otherwise the API creates one.
- Validation failures return `400`, missing resources `404`, state/config conflicts `409`, missing or invalid credentials `401`, and insufficient roles `403`.
- Secret literals are returned as `[REDACTED]`; Kubernetes Secret references never expose their values.
- List limits are integers from `1` through `100`. Cursor pagination uses `before_*_id` and returns next-cursor hints rather than page numbers.

## Roles

`GET /health` is public. Read routes require `read_only`; deploy/retry/redeploy/stop/cleanup require `deployer`; project mutation, manual deployment creation, and generic deployment patching require `admin`. GitHub webhooks use signature verification rather than operator bearer tokens. `/metrics` uses its own dedicated token.

## Health and observability

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Process liveness; public |
| `GET /health/ready` | Readiness result |
| `GET /health/db` | Safe database reachability result |
| `GET /health/platform` | Executor, authentication, repository, registry, and Kubernetes config readiness |
| `GET /health/activity` | Cross-project latest, active, failed, accepted, and ignored activity |
| `GET /health/observability` | Retention and observability posture |
| `GET /metrics` | Optional Prometheus aggregates protected by the metrics token |

`/health/activity` accepts `latest_limit`, `active_limit`, `failed_limit`, `ignored_webhook_limit`, `accepted_webhook_limit`, `deployment_status`, `webhook_status`, `project_id`, `before_deployment_id`, and `before_webhook_delivery_id`.

These endpoints expose safe control-plane facts. `/health/platform` is configuration-only and `/health/db` deliberately omits raw driver errors. Operational interpretation belongs in the [Runbook](runbook.md).

## Projects

| Method and path | Minimum role | Purpose |
| --- | --- | --- |
| `GET /api/projects` | read | List projects |
| `POST /api/projects` | admin | Create a project |
| `GET /api/projects/{project_id}` | read | Read a project |
| `PATCH /api/projects/{project_id}` | admin | Update future deployment configuration |
| `DELETE /api/projects/{project_id}` | admin | Delete a project |
| `GET /api/projects/{project_id}/status` | read | Latest, active, failed, readiness, and count summary |
| `GET /api/projects/{project_id}/activity` | read | Project deployment and webhook activity |
| `GET /api/projects/{project_id}/builds` | read | List builds |

Create requires `name`, `repo_url`, `branch`, `port`, and `healthcheck_path`. Common optional fields are `dockerfile_path`, `build_context`, `default_test_command`, `migration_command`, `trigger`, `runtime`, `git_auth_type`, `git_secret_ref`, `cpu`, `memory`, and `env_vars`.

`env_vars` entries may be literal values, ConfigMap key references, or Secret key references. See [Security](security.md#secret-handling-posture) for accepted shapes and storage tradeoffs.

Project activity accepts `latest_limit`, `webhook_limit`, `deployment_status`, `webhook_status`, `active_only`, `include_latest_failed`, `include_webhooks`, `before_deployment_id`, and `before_webhook_delivery_id`. Boolean flags accept standard `true`/`false` forms.

## Deployments and actions

| Method and path | Minimum role | Purpose |
| --- | --- | --- |
| `GET /api/projects/{project_id}/deployments` | read | Cursor-paginated deployment history |
| `GET /api/projects/{project_id}/deployments/latest` | read | Latest deployment |
| `POST /api/projects/{project_id}/deployments` | admin | Create a manual build/deployment record |
| `POST /api/projects/{project_id}/deploy` | deployer | Resolve the branch head and queue a deployment |
| `POST /api/projects/{project_id}/redeploy` | deployer | Queue from the latest deployment context |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/retry` | deployer | Retry while preserving the original test choice |
| `GET /api/projects/{project_id}/deployments/{deployment_id}` | read | Full deployment record and events |
| `PATCH /api/projects/{project_id}/deployments/{deployment_id}` | admin | Apply an allowed manual lifecycle update |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/stop` | deployer | Queue runtime stop |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/cleanup` | deployer | Queue Kubernetes/Helm resource cleanup |

The history route accepts `limit`, `deployment_status` (repeatable/comma-separated according to request parsing), and `before_deployment_id`; its response has `items` and `pagination` objects. Deploy accepts optional `branch` and `test_command`. An explicit empty test command disables tests for that deployment.

Stop and cleanup normally return `202 Accepted`: the API persists a `DeploymentCommand`, and a worker owns the runtime side effect. Retry and redeploy create new deployment history rather than rewriting the original record.

## Deployment read models

| Method and path | Purpose |
| --- | --- |
| `GET .../{deployment_id}/summary` | Compact build, deployment, URL, and error summary |
| `GET .../{deployment_id}/events` | Ordered persisted lifecycle events |
| `GET .../{deployment_id}/build-log` | Bounded build-log tail |
| `GET .../{deployment_id}/runtime-log` | Bounded runtime-log tail |
| `GET .../{deployment_id}/live-health` | Transient probe of the recorded workload URL |
| `GET .../{deployment_id}/kubernetes-diagnostics` | Workload resources, pod/container status, logs, and likely causes |

Build and runtime log routes accept a bounded `tail_lines` parameter. Live health is transient and never changes the persisted lifecycle. Kubernetes diagnostics returns a stable `not_kubernetes`-style result when the selected deployment has no Kubernetes runtime context.

## Audit events

`GET /api/audit-events` requires read access and accepts `limit` and `before_id`. Records contain timestamp, action, actor role, resource type/id, success, request ID, client IP when available, and small redacted metadata. Async worker progress belongs to deployment events, not audit records.

## GitHub webhooks

`POST /api/webhooks/github` verifies `X-Hub-Signature-256` with `CONTROL_PLANE_GITHUB_WEBHOOK_SECRET`. Configure a project with `trigger: github_push`, a matching normalized GitHub HTTPS repository, and the exact branch.

- `ping` returns `200`.
- Supported matching pushes create deployments through the normal orchestration path.
- Unsupported events/refs, unmatched repositories, branch mismatches, and duplicate `X-GitHub-Delivery` values are persisted as ignored and return `202`.
- Delivery records retain identifiers, repository, branch, commit, status/reason, time, and a deployment ID when exactly one was created; webhook bodies and secrets are not copied into audit metadata.

The webhook is intentionally not a GitHub App, general queue, or alternate deployment engine.
