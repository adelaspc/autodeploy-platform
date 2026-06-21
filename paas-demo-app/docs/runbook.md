# Runbook

## Goal

This document explains how to run the control plane locally, how to deploy it in a production-like Kubernetes setup, which configuration and secrets are required, and how to troubleshoot common operator issues.

## Runtime Components

The control plane now has three long-running runtime components plus a migration/bootstrap step:

- API: serves HTTP endpoints and health checks
- Worker: polls pending deployments and advances them through clone/build/test/push/deploy steps
- Reconciler: periodically cleans stale claims and runtime drift
- Migration job: applies Alembic/Flask-Migrate schema changes before normal runtime startup

Local Docker Compose also includes a MySQL service.

## Local Operation With Docker Compose

### Services

`docker-compose.yml` defines:

- `db`
- `migrate`
- `control-plane-api`
- `control-plane-worker`
- `control-plane-reconciler`

The normal startup flow is:

1. start MySQL
2. run migrations once
3. start API
4. start worker
5. start reconciler loop

### Local Configuration

Start from `.env.example`:

```bash
cp .env.example .env
```

Important local defaults:

- `CONTROL_PLANE_ENV=development`
- `CONTROL_PLANE_EXECUTOR=fake`
- MySQL credentials from the local `db` service

Recommended first run:

```bash
docker compose up --build
```

API endpoint:

- `http://127.0.0.1:${CONTROL_PLANE_APP_PORT:-5000}`

### Local Executor Notes

The default local Compose story is intentionally easy to evaluate:

- use `CONTROL_PLANE_EXECUTOR=fake` to exercise the control-plane API, worker, reconciliation, diagnostics, auth, audit, and deployment state machine without requiring cluster access

The Compose stack also mounts:

- `/var/run/docker.sock` into the worker and reconciler containers
- a shared workspace volume at `/tmp/paas-workspaces` for API and worker-generated deployment artifacts

That means `local-docker` mode can be enabled for a more realistic deployment path when the host Docker socket is available. In Compose, the default deploy host is set to `host.docker.internal` so health checks can reach host-published app ports from inside the worker container.

## Local Migration / Bootstrap Flow

Compose starts the `migrate` service automatically, but the same flow can be run manually:

```bash
docker compose run --rm migrate
```

If you are running outside Compose:

```bash
CONTROL_PLANE_ENV=development \
CONTROL_PLANE_DATABASE_URL=mysql+pymysql://control_plane_user:control_plane_password@127.0.0.1:3306/control_plane \
python -m flask --app wsgi:app db upgrade
```

## Health and Readiness

Public:

- `GET /health`

Protected:

- `GET /health/db`
- `GET /health/platform`
- `GET /health/activity`

Operational meaning:

- `/health` verifies the API process is serving requests
- `/health/db` verifies database reachability
- `/health/platform` shows executor/config readiness
- `/health/activity` gives an operator-oriented platform activity summary

In Compose:

- the API container has a health check against `/health`

In Kubernetes:

- the API deployment uses `/health` for readiness and liveness probes
- the worker deployment uses a minimal exec probe to confirm the long-running process is still alive

## Production-Like Kubernetes Deployment

The repository separates platform-internal deployment from user workload deployment.

The control-plane chart deploys the PaaS platform internals:

- [deploy/helm/paas-control-plane](../deploy/helm/paas-control-plane)

It deploys:

- API Deployment
- Worker Deployment
- API Service
- Reconciler CronJob
- Migration Job as a Helm hook
- ConfigMap and Secret
- shared workspace PersistentVolumeClaim
- ServiceAccount, Role, and RoleBinding for executor-facing worker and reconciler runtime

The generic web app chart documents the intended abstraction for stateless user workloads:

- [deploy/helm/generic-web-app](../deploy/helm/generic-web-app)

It deploys:

- one Deployment
- one Service
- optional Ingress

It does not include control-plane migrations, workers, PVCs, RBAC, Docker socket mounts, kubeconfig mounts, jobs, cronjobs, or framework-specific commands.

The Kubernetes executor currently still generates minimal Deployment and Service manifests directly. A future direction is:

```text
project spec -> generated values.yaml -> helm upgrade/install generic-web-app
```

The repository includes an intermediate values-generation layer for that future direction. It maps the existing project/deployment/build model to the generic chart values contract without calling Helm, calling `kubectl`, or replacing the current executor path.

Current values-generation behavior:

- uses `Build.image_ref` as the preferred Kubernetes image source
- maps `Project.port` to `container.port` and `service.port`
- maps literal env vars to `env[].value`
- maps per-variable ConfigMap and Secret key references to `env[].valueFrom`
- leaves `envFrom.configMaps` and `envFrom.secrets` empty because the current model does not support whole-resource imports
- maps `Project.healthcheck_path` to readiness and liveness probe paths for the intended Helm deployment shape
- leaves startup probes and ingress disabled by default
- does not generate runtime `command` or `args` from test or migration commands

Future Helm-managed workloads also have stable naming helpers for release names and common labels. The intended flow is:

```text
project + environment -> stable Helm release name -> helm upgrade/install generic-web-app
```

Release names use:

```text
paas-<project-slug>-<environment-slug>-<project-id-suffix>
```

The convention is one release per project/environment workload, not one release per deployment attempt. Redeploys should upgrade the same release. Stop uninstalls the same release in Helm mode. Diagnostics and reconciliation can eventually use the common PaaS workload labels/selectors.

An isolated Helm runner abstraction also exists for the Helm-mode path. It only builds and executes Helm CLI commands from primitive inputs.

The future Helm-mode flow is:

```text
project/build/deployment
  -> generated generic-web-app values
  -> stable release name
  -> HelmRunner.upgrade_install(...)
  -> one Helm release per project/environment
```

When a Helm-mode deployment is stopped, the executor uses `HelmRunner.uninstall(...)` for the same release. If the release is already absent, stop is treated as idempotently successful.

### What The Chart Assumes

The control-plane chart is intentionally minimal and assumes:

- you already have a Kubernetes cluster
- you will provide a real image repository/tag
- you will provide a real database URL
- you will provide any API tokens, webhook secret, registry credentials, and kubeconfig secret needed by your chosen executor mode
- your cluster can mount a shared workspace volume for API and worker pods

The chart creates a workspace PersistentVolumeClaim unless `workspace.existingClaim` is set. The default access mode is `ReadWriteMany` because API and worker pods need to read the same deployment log/diagnostic files. If your cluster does not provide RWX storage, point the chart at an existing claim that matches your environment.

The chart does not deploy a database. For a production-like evaluation, the recommended posture is an external MySQL-compatible database or another managed SQL endpoint that matches `CONTROL_PLANE_DATABASE_URL`.

### Helm Install Flow

Example:

```bash
helm upgrade --install paas-control-plane ./deploy/helm/paas-control-plane \
  --set image.repository=ghcr.io/example/paas-control-plane \
  --set image.tag=latest \
  --set secrets.CONTROL_PLANE_DATABASE_URL='mysql+pymysql://control_plane_user:replace-me@mysql:3306/control_plane' \
  --set secrets.CONTROL_PLANE_API_TOKEN_ADMIN='replace-me' \
  --set kubeconfig.existingSecret=paas-control-plane-kubeconfig
```

For repeatable review/demo setups, prefer a private values file instead of inline secrets.

### Local MicroK8s Registry Flow

For a single-node local MicroK8s setup, the preferred evaluation path is to push the control-plane image to the built-in registry and install the chart with the local values file:

```bash
docker build -t localhost:32000/paas-control-plane:dev .
docker push localhost:32000/paas-control-plane:dev

helm upgrade --install local ./deploy/helm/paas-control-plane \
  -n paas-local \
  --create-namespace \
  -f ./deploy/helm/paas-control-plane/values.local-microk8s.yaml \
  --set image.tag=dev
```

`values.local-microk8s.yaml` is intended for local-development clusters and currently assumes:

- `localhost:32000/paas-control-plane` as the image repository
- `image.pullPolicy=Always` to avoid stale-image ambiguity during repeated local pushes
- a hostPath-backed shared workspace at `/var/tmp/paas-control-plane-workspace`
- a shared SQLite database file at `/tmp/paas-workspaces/control_plane.db`
- an init-permissions step that `chown`s the shared workspace for the non-root app container user
- `CONTROL_PLANE_EXECUTOR=fake`, so the local Helm path validates the control-plane runtime itself without requiring Docker-socket mounts inside the worker

This file is not the production-like path. It is a local-development override for a single-node MicroK8s cluster.

### Example Config and Secret References

Examples are provided at:

- [deploy/examples/control-plane-configmap.example.yaml](../deploy/examples/control-plane-configmap.example.yaml)
- [deploy/examples/control-plane-secret.example.yaml](../deploy/examples/control-plane-secret.example.yaml)
- [deploy/examples/control-plane-kubeconfig.secret.example.yaml](../deploy/examples/control-plane-kubeconfig.secret.example.yaml)

These examples are placeholders only. Do not commit real secrets.

### Helm Validation

Validate the control-plane chart with:

```bash
helm template ci ./deploy/helm/paas-control-plane -f ./deploy/helm/paas-control-plane/values.ci.yaml > /dev/null
helm template local ./deploy/helm/paas-control-plane -f ./deploy/helm/paas-control-plane/values.local-microk8s.yaml > /dev/null
```

Validate the generic user workload chart with:

```bash
helm lint ./deploy/helm/generic-web-app
helm template generic ./deploy/helm/generic-web-app > /dev/null
helm template generic-minimal ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/minimal.yaml > /dev/null
helm template generic-node ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/node-express.yaml > /dev/null
helm template generic-python ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/python-fastapi.yaml > /dev/null
```

Generated values from the PaaS mapper can also be validated without a live Kubernetes cluster:

```bash
helm template generic ./deploy/helm/generic-web-app -f <generated-values.yaml>
```

### Kubernetes Executor Notes

The chart includes RBAC for the current Kubernetes executor surface:

- deployments
- services
- configmaps
- secrets
- pods
- pods/log
- pods/portforward

Current tradeoff:

- the control-plane image still uses local Docker build/push behavior
- the chart therefore mounts `/var/run/docker.sock` into worker and reconciler pods
- this is acceptable for a portfolio-grade demonstration but not a hardened production pattern

For `CONTROL_PLANE_EXECUTOR=kubernetes`, the worker still needs:

- Docker access for build/push
- `git`
- `kubectl`
- `helm` when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`
- a mounted kubeconfig file referenced by `CONTROL_PLANE_KUBECONFIG`
- RBAC in the target namespace

`CONTROL_PLANE_K8S_DEPLOYMENT_MODE` controls the user workload deploy and stop behavior:

- `manifest` is the default and keeps the existing direct `kubectl apply` and `kubectl delete` paths
- `helm` uses generated `generic-web-app` values, a stable release name, `HelmRunner.upgrade_install(...)` for deploy, and `HelmRunner.uninstall(...)` for stop

Reconciliation and diagnostics still use the existing direct Kubernetes resource behavior and will need separate follow-up work before Helm mode is complete. Future full Helm mode should use `helm status` and the common workload labels/selectors for those paths.

The API deployment also mounts the same kubeconfig secret path when configured so `/health/platform` reports the same readiness posture as the worker. A shared PersistentVolumeClaim is used so the API can read runtime logs and diagnostics written by the worker.

That tooling is now bundled into the image, but the Docker-socket dependency and shared-filesystem dependency remain known hardening gaps.

## Required Configuration By Mode

### Common

Required in all meaningful deployments:

- `CONTROL_PLANE_DATABASE_URL`
- `CONTROL_PLANE_ENV`

Recommended for secured deployments:

- `CONTROL_PLANE_API_TOKEN_READ_ONLY`
- `CONTROL_PLANE_API_TOKEN_DEPLOYER`
- `CONTROL_PLANE_API_TOKEN_ADMIN`
- `CONTROL_PLANE_GITHUB_WEBHOOK_SECRET`

### `fake`

Useful for evaluation/demo only:

- no registry required
- no cluster access required

### `local-docker`

Requires:

- Docker CLI in the image
- Docker socket access
- `CONTROL_PLANE_DEPLOY_HOST` reachable from the worker container or process

### `kubernetes`

Requires:

- registry push enabled
- registry URL/namespace configured
- registry credentials if the registry requires auth
- Kubernetes access (`kubectl` plus a kubeconfig file mounted from a Secret)
- RBAC in the target namespace
- a shared workspace PersistentVolumeClaim for deployment logs and diagnostics
- any referenced ConfigMaps/Secrets to already exist
- Helm CLI when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`

## Troubleshooting

### API is up but `/health/db` fails

Check:

- `CONTROL_PLANE_DATABASE_URL`
- database container/pod health
- network reachability
- credentials in local `.env`, Helm values, or Secret manifest

### Worker is not processing deployments

Check:

- worker logs
- `CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS`
- database reachability from the worker
- deployment rows are actually in `pending`

### Reconciler is not cleaning stale work

Check:

- reconciler service logs in Compose
- reconciler CronJob history in Kubernetes
- `CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS` locally
- claim TTL / refresh settings

### `local-docker` deployments fail inside containers

Check:

- Docker socket mount exists
- `CONTROL_PLANE_DEPLOY_HOST` is reachable from the worker
- the control-plane image has Docker access in the runtime environment

### `kubernetes` deployments fail before apply

Check:

- `/health/platform`
- registry push configuration
- the kubeconfig Secret is mounted and `CONTROL_PLANE_KUBECONFIG` points at that file
- referenced ConfigMaps and Secrets exist in the target namespace

### Migrations fail on startup

Check:

- DB credentials
- schema drift
- whether an old DB file or incompatible schema already exists

In Kubernetes:

- inspect the Helm migration Job logs first

## Rollback / Redeploy Notes

Current platform capabilities:

- retry a deployment
- redeploy the latest project version
- stop a deployment through the admin patch flow

Current deployment-platform tradeoffs:

- there is no full release orchestration or rollout-history manager yet
- Kubernetes cleanup is intentionally minimal
- database schema rollback is not automated in the deployment story

For platform rollout rollback:

- revert the image tag in Helm or Compose
- rerun migrations only when the target schema is compatible
- prefer forward-fix migrations over frequent down-migration workflows at this project stage

## Future Hardening

- replace Docker-socket-dependent build execution with a dedicated builder pattern
- add a dedicated readiness surface for worker and reconciler
- add chart-level support for existing shared ConfigMaps/Secrets instead of inline secret values
- support managed/external database documentation more deeply
- add image signing, scanning, and supply-chain metadata once the deployment story grows beyond evaluation/demo scope
