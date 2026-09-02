# Deployment Model

This page is the canonical source for deployment states, transitions, failure reasons, and persisted event names. Execution ownership and claim behavior are documented in [Architecture](architecture.md).

## Deployment Lifecycle

### States

```text
pending
cloning
building
testing
pushing_image
deploying
running
failed
stopped
```

Rollback states are intentionally excluded from v1. Rollback may be added later as a separate deployment action.

### Transitions

The complete transition matrix enforced by `PlatformDeployment` is:

| Current state | Allowed next states |
| --- | --- |
| `pending` | `cloning`, `failed`, `stopped` |
| `cloning` | `building`, `failed`, `stopped` |
| `building` | `testing`, `pushing_image`, `failed`, `stopped` |
| `testing` | `pushing_image`, `failed`, `stopped` |
| `pushing_image` | `deploying`, `failed`, `stopped` |
| `deploying` | `running`, `failed`, `stopped` |
| `running` | `failed`, `stopped` |
| `failed` | `stopped` |
| `stopped` | none |

Writing the current state again is accepted as an idempotent no-op. Any other transition is rejected by the model and by API validation.

The normal successful path is:

```text
pending
  → cloning
  → building
  → testing
  → pushing_image
  → deploying
  → running
```

Successful transition without tests:

```text
pending
  → cloning
  → building
  → pushing_image
  → deploying
  → running
```

Any non-terminal execution state may transition to `failed` when its current step cannot complete. A running deployment may also become `failed` when reconciliation detects that its runtime workload is missing.

Stop and cleanup transitions are intentionally broader than the normal successful path:

```text
pending | cloning | building | testing | pushing_image | deploying | running
  → stopped

failed
  → stopped  # cleanup completed after an execution or reconciliation failure
```

Retry and redeploy actions do not return a running record to `deploying`; they create a new deployment in `pending` so the existing deployment history remains immutable.

If no test command is configured for the application, the deployment skips the `testing` state and proceeds directly from `building` to `pushing_image`.

A stop requested during active processing is asynchronous and cooperatively cancels the pipeline. The deployment worker observes the persisted stop command at a step boundary or executor heartbeat, releases its claim without marking the deployment failed, and leaves cleanup plus the `stopped` transition to the command worker. The interrupted build becomes `cancelled` rather than remaining in an active state. A step that is already executing may finish before its next heartbeat, but later pipeline steps do not begin after cancellation is acknowledged.

### Build states

Build records use `pending`, `cloning`, `building`, `testing`, `pushing_image`, `succeeded`, `failed`, and `cancelled`. `cancelled` is terminal and is used when an operator stop interrupts an active deployment pipeline; it is distinct from an execution failure.

## Failure Reasons

A deployment may fail because of:

- repository clone failure
- missing or invalid Dockerfile
- Docker build failure
- test command failure
- image push failure
- Kubernetes manifest apply failure
- rollout timeout
- healthcheck failure after rollout

## Deployment Flow

```text
Operator submits project configuration and requests a deployment
        ↓
API validates the request and persists Build + Deployment intent
        ↓  HTTP response returns; no infrastructure command runs in the request
Worker atomically claims the pending deployment
        ↓
Worker invokes the selected executor contract for each pipeline step
        ├─ fake
        │    simulate checkout, build, test, publish, deploy, and health results
        │    without a repository checkout, container image, registry, or runtime
        │
        ├─ local-docker
        │    checkout source → build image → optional tests
        │    → optional registry push and verification
        │    → run local container → direct HTTP healthcheck
        │
        └─ kubernetes
             checkout source → build image → optional tests
             → required registry push and remote image verification
             → referenced-resource preflight
             → generated manifests or Helm release
             → rollout wait → Service port-forward healthcheck
        ↓
Worker persists status, events, logs, diagnostics, and runtime metadata
        ↓
API exposes the persisted result to the operator console
```

The API owns validation, authorization, and persisted intent. The worker owns execution and all calls to Git, Docker, the registry, Kubernetes, and Helm. Executor-specific steps still use the common lifecycle; unsupported operations are recorded as skipped or simulated rather than being silently attributed to another component.

## Event Model

Each major step should produce a persisted deployment event.

Example events:

- `deployment.created`
- `repository.clone_started`
- `repository.clone_failed`
- `image.build_started`
- `image.build_failed`
- `image.build_succeeded`
- `tests.started`
- `tests.failed`
- `tests.succeeded`
- `image.push_started`
- `image.push_failed`
- `image.push_succeeded`
- `image.verify_started`
- `image.verify_failed`
- `image.verify_succeeded`
- `kubernetes.manifest_apply_started`
- `kubernetes.manifest_apply_succeeded`
- `kubernetes.manifest_apply_failed`
- `kubernetes.rollout_started`
- `kubernetes.rollout_succeeded`
- `kubernetes.rollout_failed`
- `kubernetes.healthcheck_started`
- `kubernetes.healthcheck_succeeded`
- `kubernetes.healthcheck_failed`
- `kubernetes.helm_deploy_started`
- `kubernetes.helm_deploy_succeeded`
- `kubernetes.helm_deploy_failed`
- `deployment.running`
- `deployment.failed`
- `deployment.stopped`
