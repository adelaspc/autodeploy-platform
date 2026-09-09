# API Reference

The JSON API is rooted at `/api`; health and metrics endpoints are rooted at `/`. This page is the canonical route and query-parameter index. Domain lifecycle semantics are documented in [Deployment Model](deployment.md), and authentication details in [Security](security.md).

## Conventions

- Send JSON request bodies with `Content-Type: application/json`. Every endpoint that consumes a JSON body requires a top-level object; valid arrays, strings, numbers, booleans, and `null` return `400 {"error":"Request body must be a JSON object"}`. An absent body remains equivalent to an empty object for routes whose fields are optional.
- When API authentication is configured, send `Authorization: Bearer <token>`.
- Every response includes `X-Request-ID`. A valid client-supplied `X-Request-ID` is reused; otherwise the API creates one.
- Validation failures and invalid lifecycle transitions return `400`; missing resources return `404`; state/config conflicts return `409`; missing or invalid credentials return `401`; and insufficient roles return `403`.
- Secret literals are returned as `[REDACTED]`; Kubernetes Secret references never expose their values.
- List and cursor limits are integers from `1` through `100`. Cursor pagination uses `before_*_id` and returns next-cursor hints rather than page numbers. Log-tail requests use their separate `tail_lines` bound described below.

## Roles

`GET /health` and `GET /health/ready` are public. Other read routes require `read_only`; deploy/retry/redeploy/stop/cleanup require `deployer`; project mutation, manual deployment creation, and generic deployment patching require `admin`. GitHub webhooks use signature verification rather than operator bearer tokens. `/metrics` uses its own dedicated token.

## Health and observability

| Method and path | Purpose |
| --- | --- |
| `GET /health` | Process liveness; public |
| `GET /health/ready` | Readiness result; public |
| `GET /health/db` | Safe database reachability result |
| `GET /health/platform` | Executor, authentication, repository, registry, and Kubernetes config readiness |
| `GET /health/activity` | Cross-project latest, active, failed, accepted, and ignored activity |
| `GET /health/observability` | Logging, metrics, and request-correlation posture |
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

Create and patch payloads reject unknown fields. String inputs are bounded to their persisted model sizes; `port` must be a JSON integer rather than a boolean. CPU accepts positive core or milliCPU values such as `1`, `0.5`, and `250m` with at most milliCPU precision. Memory accepts positive whole-byte quantities with an optional decimal or binary suffix, such as `100M`, `512Mi`, or `1Gi`.

`DELETE /api/projects/{project_id}` returns `200 {"message":"Project deleted"}` after deleting persisted project-owned builds, deployments, events, and commands. It does not queue runtime stop or cleanup first; stop or clean the workload before deleting the project.

Project activity accepts `latest_limit`, `webhook_limit`, `deployment_status`, `webhook_status`, `active_only`, `include_latest_failed`, `include_webhooks`, `before_deployment_id`, and `before_webhook_delivery_id`. Boolean flags accept standard `true`/`false` forms.

## Deployments and actions

| Method and path | Minimum role | Purpose |
| --- | --- | --- |
| `GET /api/projects/{project_id}/deployments` | read | Cursor-paginated deployment history |
| `GET /api/projects/{project_id}/deployments/latest` | read | Latest deployment |
| `POST /api/projects/{project_id}/deployments` | admin | Create a manual build/deployment record |
| `POST /api/projects/{project_id}/deploy` | deployer | Resolve the branch head and queue a deployment |
| `POST /api/projects/{project_id}/redeploy` | deployer | Queue the latest deployment branch with current project inputs |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/retry` | deployer | Repeat the selected deployment's persisted execution inputs |
| `GET /api/projects/{project_id}/deployments/{deployment_id}` | read | Full deployment record and events |
| `PATCH /api/projects/{project_id}/deployments/{deployment_id}` | admin | Apply an allowed manual lifecycle update |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/stop` | deployer | Queue runtime stop |
| `POST /api/projects/{project_id}/deployments/{deployment_id}/cleanup` | deployer | Queue Kubernetes/Helm resource cleanup |

The history route accepts `limit`, `deployment_status` (repeatable/comma-separated according to request parsing), and `before_deployment_id`; its response has `items` and `pagination` objects. Deploy accepts optional `branch` and `test_command`. If `test_command` is omitted, the project default is inherited; an explicit `null` disables tests for that deployment; an empty string is invalid and returns `400`. Shell-control characters and shell-wrapper commands are also rejected with `400`.

All `limit` parameters accept integers from `1` through `100`. Deployment history and audit history default to `20`; project activity, health activity, and their nested lists default to `10`. Cursor parameters such as `before_deployment_id` and `before_webhook_delivery_id` are positive integer IDs; use the returned pagination cursor unchanged for the next page.

### Manual deployment records

`POST /api/projects/{project_id}/deployments` is an admin-only fixture and recovery interface. It creates persisted records but does not build, publish, or deploy a workload. `commit_sha` is required and must be a hexadecimal Git object ID: an abbreviated 7–40 character SHA-1 identifier or a complete 64-character SHA-256 identifier. It is normalized to lowercase. `status` and `build_status` default to `pending`; `environment` defaults to `production`; `test_command` inherits the project default unless explicitly set to `null`. Unknown fields and values whose types or lengths do not match the persisted model are rejected with `400`; `service_url`, when provided, must be an absolute HTTP or HTTPS URL.

```json
{
  "commit_sha": "0123456789abcdef0123456789abcdef01234567",
  "registry": "registry.example.com/team",
  "image_name": "example-app",
  "image_tag": "manual-check",
  "status": "pending",
  "build_status": "pending",
  "environment": "production",
  "test_command": "pytest -q",
  "message": "Manual verification record"
}
```

`status` must be one of `pending`, `cloning`, `building`, `testing`, `pushing_image`, `deploying`, `running`, `failed`, or `stopped`. `build_status` must be one of `pending`, `cloning`, `building`, `testing`, `pushing_image`, `succeeded`, `failed`, or `cancelled`. A successful response is `201` and contains the full deployment, build, and initial `deployment.created` event. A missing `commit_sha` returns `400 {"error":"Missing required field: commit_sha"}`.

```json
{
  "id": 42,
  "project_id": 7,
  "status": "pending",
  "environment": "production",
  "build": {
    "commit_sha": "0123456789abcdef0123456789abcdef01234567",
    "status": "pending",
    "image_ref": "registry.example.com/team/example-app:manual-check"
  },
  "events": [{"event_type": "deployment.created", "status": "pending"}]
}
```

Stop and cleanup normally return `202 Accepted`: the API persists a `DeploymentCommand`, and a worker owns the runtime side effect. Setting a deployment status to `stopped` through the generic patch also queues the stop action and returns `202`; other invalid lifecycle transitions return `400`, while state/config conflicts return `409`. Retry and redeploy create new deployment history rather than rewriting the original record.

Deploy, retry, and redeploy deliberately use different input sources:

| Action | Branch | Commit | Project execution settings | Test command | Environment and image name |
| --- | --- | --- | --- | --- | --- |
| Deploy | Request override, otherwise current project | Current head resolved when the request is accepted | Current project snapshot | Request value, including explicit `null`; otherwise current project default | Current defaults |
| Retry | Selected deployment snapshot | Selected deployment build commit | Exact copy of the selected deployment snapshot | Exact selected build value, including `null` | Exact selected deployment environment and build image name |
| Redeploy | Latest deployment snapshot | Current head resolved when the request is accepted | New snapshot of the current project | Current project default | Current defaults |

Retry does not resolve the branch head. It returns `409` and recommends redeploy when the historical snapshot or commit is missing, has an unsupported version, is incomplete, or does not belong to the requested project. A successful deploy/retry/redeploy response includes `creation_action` and `configuration_source`; retry and redeploy also include their source deployment ID. The Summary view exposes the same provenance from the persisted `deployment.created` event.

For the generic deployment PATCH route, a normal permitted status or build-status update returns `200` with the updated deployment. Setting `status` to `stopped` is the exception: it creates a `DeploymentCommand`, returns `202`, and leaves runtime cleanup to the worker. Stop and cleanup commands may later be `succeeded`, `failed`, or `skipped`; a Helm command is skipped when a newer deployment owns the shared release.

Historical retry reproduces the inputs controlled by a deployment record, not every external dependency. Symbolic Secret/ConfigMap references can resolve to newer values, the recorded repository and commit must remain available, and rebuilding can produce another digest when base images or package sources changed. Current platform registry, credentials, executor, cluster, Helm chart, and runtime policy still apply.

## Deployment read models

| Method and path | Purpose |
| --- | --- |
| `GET .../{deployment_id}/summary` | Compact build, deployment, URL, and error summary |
| `GET .../{deployment_id}/events` | Ordered persisted lifecycle events |
| `GET .../{deployment_id}/build-log` | Bounded build-log tail |
| `GET .../{deployment_id}/runtime-log` | Bounded runtime-log tail |
| `GET .../{deployment_id}/live-health` | Transient control-plane API probe of the recorded workload URL |
| `GET .../{deployment_id}/kubernetes-diagnostics` | Workload resources, pod/container status, logs, and likely causes |

Build and runtime log routes accept a bounded `tail_lines` parameter from `1` through `2000`, defaulting to `200`. Live health is transient and never changes the persisted lifecycle. Its HTTP request originates from the control-plane API environment, so `healthy` does not prove access from the operator's browser; use **Open service in browser** for that manual check. Kubernetes diagnostics is a read of persisted deployment events and never invokes Kubernetes; use live health, the workload URL, or scoped `kubectl` for current runtime state. It returns `404` with `{"error": "Deployment does not use the Kubernetes target"}` when the selected deployment has no Kubernetes runtime context.

Deployment summaries retain the compatibility booleans `build_log_available` and `runtime_log_available` and also expose `build_log_state` and `runtime_log_state`. Each state is `available`, `not_produced`, or `retention_removed`. Explicit retention records `observability.artifacts_removed` with the removed log classes and cutoff; it does not remove lifecycle history. Log endpoints keep their existing behavior and return `404` when no retained file can be served.

Kubernetes deployment records and summaries expose `kubernetes_deployment_mode`, `kubernetes_namespace`, and the exact `kubernetes_deployment_name`, `kubernetes_service_name`, and `kubernetes_ingress_name` values for manifest-managed attempts. These are persisted runtime identity, not a projection of the platform's current configuration or naming rules. Stop, cleanup, status inspection, and reconciliation use the persisted values.

Deployment events cover lifecycle, worker commands, reconciliation, webhook processing, and observability retention. Clients must tolerate new event types and additional metadata fields.

### Helm failure diagnostic snapshots

For `kubernetes.helm_deploy_failed`, `failure_stage` remains `helm` and `failure_summary` preserves the Helm error. Available snapshot data populates `pod_names`, `pod_phase`, `container_reason`, `restart_count`, `pod_runtime`, `images`, `image_pull_secrets`, `pod_describe_summary`, `pod_logs_summary`, and `pod_previous_logs_summary`. `deployment_describe_summary` and `service_describe_summary` expose bounded resource descriptions, including available event details.

Additional fields:

| Field | Meaning |
| --- | --- |
| `diagnostics_snapshot_at` | UTC ISO-8601 time of the stored evidence, or `null` when an older record has no capture time. |
| `diagnostics_collected_at` | UTC ISO-8601 snapshot collection start time, or `null` for older records. |
| `diagnostics_collection_status` | `complete`, `partial`, `unavailable`, or `null` when no collection status was recorded. |
| `diagnostics_collection_errors` | Up to 12 objects with `operation` and a safe `reason`; empty when no errors were recorded. |

Reasons include `access_denied`, `resource_not_found`, `no_matching_pods`, `previous_logs_unavailable`, `command_timed_out`, `collection_budget_exhausted`, `pod_limit_reached`, `command_unavailable`, `command_failed`, `invalid_resource_response`, `log_write_failed`, and `collection_failed`. Missing evidence is not an application log or a second deployment failure. Old events remain readable without backfilled diagnostics. Refreshing this endpoint reads persisted data and does not query Kubernetes.

## Audit events

`GET /api/audit-events` requires read access and accepts `limit` and `before_id`. Records contain timestamp, action, actor role, resource type/id, success, request ID, client IP when available, and small redacted metadata. Async worker progress belongs to deployment events, not audit records.

## GitHub webhooks

`POST /api/webhooks/github` verifies `X-Hub-Signature-256` with `CONTROL_PLANE_GITHUB_WEBHOOK_SECRET`. Configure a project with `trigger: github_push`, a matching normalized GitHub HTTPS repository, and the exact branch.

- `ping` returns `200`.
- Supported matching pushes create deployments through the normal orchestration path.
- Unsupported events/refs, deleted branches, unmatched repositories, branch mismatches, and duplicate `X-GitHub-Delivery` values are persisted as ignored and return `202`.
- Delivery records retain identifiers, repository, branch, commit, status/reason, time, and a deployment ID when exactly one was created; webhook bodies and secrets are not copied into audit metadata.

The webhook is intentionally not a GitHub App, general queue, or alternate deployment engine.


### Image reference lifecycle

The existing build and deployment-summary `image_tag` and `image_ref` fields retain their types. Before build/push, `image_ref` may be a planned or tagged reference. After a successful registry push, `image_ref` is the digest reference reported by that push; `image.verify` checks this exact reference before rollout. `image_tag` remains the local, commit-prefixed unique build tag. The `image.push_succeeded` event includes `tagged_image_ref` and `image_digest` in its metadata. The Summary panel displays the build tag and deployment image separately. No database migration or historical backfill is required.

Registry-disabled and historical records can still contain tag references. A digest reference alone does not imply successful verification: consult deployment status and the `image.verify` event. Missing or ambiguous push digests fail `image.push`; unpinned registry references fail `image.verify` instead of silently falling back to a tag.
