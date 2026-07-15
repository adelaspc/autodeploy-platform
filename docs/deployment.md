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

Successful transition:

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

Failure transitions:

```text
pending → failed
cloning → failed
building → failed
testing → failed
pushing_image → failed
deploying → failed
running → failed
running → stopped
```

If no test command is configured for the application, the deployment skips the `testing` state and proceeds directly from `building` to `pushing_image`.

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
User submits app config
        ↓
Control Plane creates app and deployment record
        ↓
Platform Worker clones GitHub repository
        ↓
Platform Worker builds Docker image
        ↓
Platform Worker optionally runs tests
        ↓
Platform Worker pushes image to registry
        ↓
Platform Worker verifies remote image with Docker Buildx
        ↓
Control Plane deploys image_ref to Kubernetes
        ↓
Kubernetes rolls out application
        ↓
Healthcheck verifies application
        ↓
Control Plane updates deployment status
```

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
