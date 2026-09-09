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

Create local ignored environment profiles:

```bash
make env-init
```

This creates local files that are ignored by Git:

- `.env.demo`: local demo profile using the fake executor
- `.env.local-docker`: local Docker executor profile
- `.env.local-kubernetes`: local Kubernetes or Helm executor profile
- `.env.secrets`: local tokens, passwords, and API secrets

Each local file is copied from its committed `.example` counterpart. `make env-init` never overwrites an existing local file, generates missing local MySQL passwords, and restricts all generated environment files to mode `0600`. Keep runtime configuration in the profile files and all credentials in `.env.secrets`. If local profile files already exist, copy newly added keys such as `CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED` from the matching `.example` file manually.

The Makefile loads `.env.<profile>` and `.env.secrets` together as Compose interpolation inputs, so you do not need to manually export variables. Compose then maps credentials explicitly: API secrets to the API, registry credentials to the worker, Git credentials to API and worker, and only the database URL to migration and reconciliation.

Important demo defaults:

- `CONTROL_PLANE_ENV=development`
- `CONTROL_PLANE_EXECUTOR=fake`
- no real registry, Git, or API secrets required

For private Git repositories, set a token in `.env.secrets` using the project's `git_secret_ref`. The committed Compose mapping supports `GITHUB` by default because that is the documented example:

```bash
CONTROL_PLANE_GIT_TOKEN_GITHUB=replace-me
```

If you use another reference such as `GITLAB`, add `CONTROL_PLANE_GIT_TOKEN_GITLAB` to both the API and worker `environment` mappings through a Compose override. Merely adding an arbitrary key to `.env.secrets` does not inject it into a container.

Recommended first run:

```bash
make compose-up PROFILE=demo
```

API endpoint:

- `http://127.0.0.1:${CONTROL_PLANE_APP_PORT:-5000}`

### Local Executor Notes

The default local Compose story is intentionally easy to evaluate:

- use `CONTROL_PLANE_EXECUTOR=fake` to exercise the control-plane API, worker, reconciliation, diagnostics, auth, audit, and deployment state machine without requiring cluster access

The Compose stack also uses:

- a shared workspace volume at `/tmp/paas-workspaces` for API and worker-generated deployment artifacts
- an optional `docker-compose.docker-socket.yml` override for `/var/run/docker.sock` when `CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED=true`

That means `local-docker` mode can be enabled for a more realistic deployment path when the host Docker socket is available. Treat this as a trusted local boundary: Docker socket access is effectively host-level Docker daemon access and should not be used with untrusted repositories, Dockerfiles, test commands, or workload images. In Compose, the default deploy host is set to `host.docker.internal` so health checks can reach host-published app ports from inside the worker container.

### Local Stack Lifecycle

Use the Makefile targets consistently with the same profile used at startup:

```bash
make compose-logs PROFILE=demo
make compose-down PROFILE=demo
```

`compose-down` stops the stack but preserves MySQL and workspace volumes. Generated kubeconfig copies and other transient files can be removed independently:

```bash
make runtime-clean
```

For an intentionally destructive reset, including the Compose database and workspace volumes, use the explicit confirmation guard:

```bash
make compose-reset PROFILE=demo CONFIRM=reset
```

This reset deletes local control-plane database state, deployment artifacts stored in the Compose workspace volume, and generated `.runtime` files. It does not delete workload resources already deployed into Kubernetes; clean those through the deployment cleanup action before resetting the control plane.

## Local Migration / Bootstrap Flow

Compose starts the `migrate` service automatically, but the same flow can be run manually:

```bash
make db-upgrade PROFILE=demo
```

If you are running outside Compose:

```bash
make db-upgrade PROFILE=demo
```

## Health and Readiness

Public:

- `GET /health`
- `GET /health/ready`

Protected:

- `GET /health/db`
- `GET /health/platform`
- `GET /health/activity`
- `GET /health/observability`

Operational meaning:

- `/health` verifies the API process is serving requests
- `/health/ready` verifies database reachability for orchestrator readiness without exposing database details
- `/health/db` verifies database reachability and returns only a safe status/error code; driver details, paths, hosts, and credentials are kept out of the response
- `/health/platform` shows executor/config readiness
- `/health/activity` gives an operator-oriented platform activity summary
- `/health/observability` reports the safe runtime posture for metrics, logging, and request correlation without exposing secrets

For a selected running deployment with a public service URL, the browser polls `GET /api/projects/<id>/deployments/<deployment_id>/live-health`, and the control-plane API performs the actual HTTP probe. This live signal therefore confirms reachability from the API environment, not from the operator's browser. It is intentionally separate from the persisted deployment lifecycle status: a transient Pod replacement can show `unhealthy` and then recover to `healthy` without rewriting the deployment as failed. Use **Open service in browser** to check access from the operator's machine.

In Compose:

- the API container has a dependency-aware health check against `/health/ready`

In Kubernetes:

- the API deployment uses `/health` for liveness and `/health/ready` for readiness
- the worker liveness probe confirms the long-running process is alive, while readiness runs `check-worker-readiness` to verify database access and executor configuration

## Metrics and Structured Logs

Application logs use JSON on stdout by default. `CONTROL_PLANE_COMPONENT` identifies API, worker, and reconciler records in Docker Compose. The long-running worker and reconciler lifecycle and result messages use the same structured logger; one-shot CLI commands retain human-readable terminal output. Request completion records also contain `request_id`, method, path, status, and duration.

The Prometheus-compatible metrics endpoint is optional and disabled by default:

```env
CONTROL_PLANE_METRICS_ENABLED=true
```

Set its dedicated secret in `.env.secrets`:

```env
CONTROL_PLANE_METRICS_TOKEN=replace-with-a-dedicated-secret
```

Verify it locally:

```bash
curl -fsS \
  -H "Authorization: Bearer ${CONTROL_PLANE_METRICS_TOKEN}" \
  "http://127.0.0.1:${CONTROL_PLANE_APP_PORT:-5000}/metrics"
```

Operational behavior:

- disabled endpoint: `404`
- missing or invalid dedicated bearer token: `401`
- enabled and authenticated endpoint: Prometheus text exposition
- the endpoint performs small aggregate database queries and does not expose raw records
- metric labels use only bounded status/result/level values

For a Helm deployment, set `config.CONTROL_PLANE_METRICS_ENABLED` and provide `secrets.values.CONTROL_PLANE_METRICS_TOKEN` when the chart creates the runtime Secret. The chart does not install Prometheus or create a `ServiceMonitor`.

## Observability Retention

Retention is an explicit operator action. Preview cleanup candidates older than 30 days:

```bash
.venv/bin/python -m flask --app wsgi:app cleanup-observability --older-than-days 30
```

Apply the cleanup after reviewing the dry-run:

```bash
.venv/bin/python -m flask --app wsgi:app cleanup-observability --older-than-days 30 --apply
```

The default cleanup removes workspace/build-log artifacts for old `failed` and `stopped` deployments and prunes only disposable `claim_*` and `reconcile.*` events. It preserves deployment lifecycle events, deployment/build rows, and webhook delivery records. Audit events are retained unless `--include-audit-events` is supplied explicitly together with `--apply`.

Failure reconciliation removes supported orphan runtime resources, but it does not remove the failed deployment's workspace or raw logs. This separation leaves evidence available for investigation until an operator applies retention. After retention removes a workspace, the deployment summary reports the affected build/runtime log state as `retention_removed`; persisted lifecycle events and bounded Kubernetes diagnostic summaries remain readable. Export required raw evidence before applying deletion because workspace files cannot be reconstructed from the database.

## Production-Like Kubernetes Deployment

The repository separates platform-internal deployment from user workload deployment.

The control-plane chart deploys the PaaS platform internals:

- [deploy/helm/autodeploy-control-plane](../deploy/helm/autodeploy-control-plane)

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

The Kubernetes executor supports two workload deployment modes:

- `manifest`: default; generates Deployment, Service, and optional Ingress manifests directly, then uses `kubectl apply` and `kubectl delete`
- `helm`: generates `generic-web-app` values, installs or upgrades a stable Helm release, and uninstalls that release on stop

The repository includes a values-generation layer for Helm mode. It maps the existing project/deployment/build model to the generic chart values contract before the executor calls Helm.

Current values-generation behavior:

- uses `Build.image_ref` as the preferred Kubernetes image source
- maps `Project.port` to `container.port` and `service.port`
- maps literal env vars to `env[].value`
- maps per-variable ConfigMap and Secret key references to `env[].valueFrom`
- leaves `envFrom.configMaps` and `envFrom.secrets` empty because the current model does not support whole-resource imports
- maps `Project.healthcheck_path` to readiness and liveness probe paths for Helm-managed workloads
- leaves startup probes and ingress disabled by default
- does not generate runtime `command` or `args` from test or migration commands

Helm-managed workloads use stable naming helpers for release names and common labels. The flow is:

```text
project + environment -> stable Helm release name -> helm upgrade/install generic-web-app
```

Release names use:

```text
paas-<project-slug>-<environment-slug>-<project-id-suffix>
```

Manifest-managed workloads use one DNS-1123-safe, attempt-specific base name:

```text
Deployment/Ingress: paas-<project-slug>-<deployment-id>
Service:            paas-<project-slug>-<deployment-id>-svc
```

The project slug is shortened as needed so the Service, including its suffix, remains within 63 characters. The exact names are stored on the deployment record; use the diagnostics/API values for operational commands instead of reconstructing them. Migrated records retain names produced by the historical rule.

The convention is one release per project/environment workload, not one release per deployment attempt. Redeploys should upgrade the same release. Stop uninstalls the same release in Helm mode. Deployment rows are mutable lifecycle records with immutable specification snapshots; the newest row for a project and environment owns the shared runtime release.

Automatic Helm cleanup is ownership-aware. The reconciler refreshes its database view before cleanup and skips every failed or stopped record that has a newer deployment for the same project and environment. This matters with MySQL repeatable-read transactions: a reconciliation cycle may have started before a concurrent redeployment was created, but it must not uninstall the release after that redeployment upgrades it.

An isolated Helm runner abstraction exists for the Helm-mode path. It only builds and executes Helm CLI commands from primitive inputs.

The Helm-mode flow is:

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

The chart supports exactly one of two runtime Secret modes:

- `secrets.create=true` and an empty `secrets.existingSecret`: the chart creates and manages the runtime Secret from `secrets.values`; this is the convenient local-demo mode.
- `secrets.create=false` and a non-empty `secrets.existingSecret`: components select their permitted keys from an externally managed Secret; the chart does not create, modify, or delete it.

The chart rejects configurations where both modes are active or neither mode is configured.

For a local trusted environment, copy the committed example to the ignored local filename and restrict its permissions:

```bash
cp deploy/helm/autodeploy-control-plane/values.secrets.local.example.yaml values.secrets.local.yaml
chmod 0600 values.secrets.local.yaml
```

Edit `values.secrets.local.yaml`, then install using the local secret values file. The command line should contain only non-secret settings:

```bash
helm upgrade --install autodeploy-control-plane ./deploy/helm/autodeploy-control-plane \
  --set image.repository=ghcr.io/example/autodeploy-control-plane \
  --set image.tag=latest \
  --values values.secrets.local.yaml \
  --set kubeconfig.existingSecret=autodeploy-control-plane-kubeconfig
```

`values.secrets.local.yaml` contains plaintext credentials. It is ignored by Git, but that does not encrypt or otherwise protect its contents; keep it local, use mode `0600`, and use it only in a controlled, trusted environment. Do not pass credentials through `--set`: they may be retained in shell history or exposed through process inspection.

A Kubernetes Secret is base64-encoded, not strongly encrypted by default. Its effective protection depends on cluster RBAC and, when configured, encryption at rest for etcd. Users or service accounts with sufficient cluster or namespace permissions can read Secret values. Helm also stores release information in the cluster; values supplied through `values.secrets.local.yaml` can therefore be recovered by principals with sufficient access to Helm release storage.

For a shared or production-like environment, create the runtime Secret through the external secret-management workflow and configure only its name in Helm values:

```yaml
secrets:
  create: false
  existingSecret: autodeploy-control-plane-runtime
  gitTokenKeys:
    - CONTROL_PLANE_GIT_TOKEN_GITHUB
  values: {}
```

Omit `gitTokenKeys` when private Git access is not needed. In external-Secret mode the list tells the chart which existing keys to expose to API and worker; Helm cannot discover them from the Secret. Each entry must follow `CONTROL_PLANE_GIT_TOKEN_<REF>`. With `secrets.create=true`, non-empty matching keys in `secrets.values` are detected automatically.

In both modes the chart uses individual `secretKeyRef` selectors rather than importing the whole Secret. API receives its auth/webhook/metrics keys and configured Git tokens, worker receives registry credentials and configured Git tokens, while reconciler and migration receive only the database URL. Helm stores the external Secret name and Git key names, but not the externally managed values. External Secrets, SOPS, Vault, or the cluster operator's standard secret-management mechanism can own those values and their rotation.

### Local MicroK8s Registry Flow

For a single-node local MicroK8s setup, the preferred evaluation path is to push the control-plane image to the built-in registry and install the chart with the local values file:

```bash
docker build -t localhost:32000/autodeploy-control-plane:dev .
docker push localhost:32000/autodeploy-control-plane:dev

helm upgrade --install local ./deploy/helm/autodeploy-control-plane \
  -n paas-local \
  --create-namespace \
  -f ./deploy/helm/autodeploy-control-plane/values.local-microk8s.yaml \
  --set image.tag=dev
```

`values.local-microk8s.yaml` is intended for local-development clusters and currently assumes:

- `localhost:32000/autodeploy-control-plane` as the image repository
- `image.pullPolicy=Always` to avoid stale-image ambiguity during repeated local pushes
- a hostPath-backed shared workspace at `/var/tmp/autodeploy-control-plane-workspace`
- a shared SQLite database file at `/tmp/paas-workspaces/control_plane.db`
- an init-permissions step that `chown`s the shared workspace for the non-root app container user
- `CONTROL_PLANE_EXECUTOR=fake`, so the local Helm path validates the control-plane runtime itself without deploying workloads
- no Docker socket mount, because this chart validation profile uses the fake executor

This file is not the production-like path. It is a local-development override for a single-node MicroK8s cluster.

### Example Config and Secret References

Examples are provided at:

- [deploy/examples/control-plane-configmap.example.yaml](../deploy/examples/control-plane-configmap.example.yaml)
- [deploy/helm/autodeploy-control-plane/values.secrets.local.example.yaml](../deploy/helm/autodeploy-control-plane/values.secrets.local.example.yaml)
- [deploy/examples/control-plane-existing-secret.example.yaml](../deploy/examples/control-plane-existing-secret.example.yaml)
- [deploy/examples/control-plane-kubeconfig.secret.example.yaml](../deploy/examples/control-plane-kubeconfig.secret.example.yaml)

These examples are placeholders only. Do not commit real secrets.

### Helm Validation

Validate the control-plane chart with:

```bash
helm template ci ./deploy/helm/autodeploy-control-plane -f ./deploy/helm/autodeploy-control-plane/values.ci.yaml > /dev/null
helm template local ./deploy/helm/autodeploy-control-plane -f ./deploy/helm/autodeploy-control-plane/values.local-microk8s.yaml > /dev/null
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

#### User workload resources and probes

Manifest and Helm modes apply the same project workload settings. `cpu` and `memory` create optional container `resources.requests`; they do not create limits. The configured `healthcheck_path` creates readiness and liveness HTTP probes on the named `http` container port. Readiness starts after 5 seconds and repeats every 10 seconds; liveness starts after 15 seconds and repeats every 20 seconds. Startup probes remain disabled.

After a deployment, inspect the exact workload name shown in Kubernetes Diagnostics:

```bash
kubectl get deployment <deployment-name> --namespace default -o yaml
```

Confirm that the rendered container includes `resources.requests`, `readinessProbe`, and `livenessProbe`. Repeat the inspection once in manifest mode and once in Helm mode with the same project values. The container contract must match; only the resource management path and stable naming differ.

The chart includes RBAC for the current Kubernetes executor surface:

- read-only preflight for referenced ConfigMaps and Secrets
- read-only Pod inspection and Pod logs for diagnostics
- temporary Service port-forward creation for healthchecks
- create/update/patch/delete for managed Services, Deployments, and optional Ingresses
- read-only Events access for operator diagnostics

On a successful deployment, the worker runs `kubectl logs deployment/<name> --all-containers=true --prefix=true --tail=200` and stores the redacted output as the deployment runtime-log snapshot. A logging failure does not turn a healthy rollout into a failed deployment; its error is retained in the snapshot for troubleshooting. Redeploy to capture a new snapshot, or use `kubectl logs` directly for live/current output.

The default Role is intentionally scoped for manifest mode and does not grant Secret or ConfigMap mutation. Helm mode may need additional Secret mutation permissions because Helm v3 stores release metadata in Secrets by default. Enable that explicitly with:

```yaml
rbac:
  helmReleaseStorage: true
```

`IngressClass` is cluster-scoped, so it is not granted by the namespace Role. When `CONTROL_PLANE_K8S_INGRESS_CLASS_NAME` is set, the configured kubeconfig or ServiceAccount must already be allowed to read the referenced IngressClass, or preflight will report it as missing/unreadable.

When the Kubernetes executor uses the ServiceAccount created by this chart, `CONTROL_PLANE_K8S_NAMESPACE` must match the Helm release namespace because the Role and RoleBinding are namespace-scoped. The chart rejects a different workload namespace in this mode. To target a separately authorized namespace, provide `kubeconfig.existingSecret`; the worker and reconciler then use that kubeconfig instead of the in-cluster ServiceAccount token.

Current tradeoff:

- the control-plane image still uses local Docker build/push behavior
- the base chart does not mount `/var/run/docker.sock` by default
- `values.local-microk8s.yaml` keeps the Docker socket disabled because its executor is `fake`
- Docker socket access is acceptable for a portfolio-grade local demonstration but not a hardened production pattern

If a trusted single-node installation intentionally builds through the host Docker daemon, enable the mount and provide the socket group ID so the non-root worker and reconciler can access it:

```yaml
dockerSocket:
  enabled: true
  hostPath: /var/run/docker.sock
  groupId: "<host-docker-socket-gid>"
```

Obtain the value on the node with `stat -c '%g' /var/run/docker.sock`. This is a local-only escape hatch; the recommended real Kubernetes demo remains the Compose worker connected to MicroK8s.

For `CONTROL_PLANE_EXECUTOR=kubernetes`, the worker still needs:

- Docker access for build/push when using the current local builder path
- `git`
- `kubectl`
- `helm` when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`; the executor passes `CONTROL_PLANE_KUBECONFIG` explicitly to Helm with `--kubeconfig`
- a host kubeconfig file mounted into the worker container
- `CONTROL_PLANE_KUBECONFIG_HOST` pointing at the host file and `CONTROL_PLANE_KUBECONFIG` pointing at its in-container path
- container group membership for the kubeconfig file group, applied by the Compose Makefile targets
- RBAC in the target namespace

`CONTROL_PLANE_K8S_DEPLOYMENT_MODE` controls the user workload deploy and stop behavior:

- `manifest` is the default and keeps the existing direct `kubectl apply` and `kubectl delete` paths
- `helm` uses generated `generic-web-app` values, a stable release name, `HelmRunner.upgrade_install(...)` for deploy, and `HelmRunner.uninstall(...)` for stop

Helm mode can be tuned with `CONTROL_PLANE_K8S_HELM_CHART_PATH`, `CONTROL_PLANE_K8S_HELM_BINARY`, and `CONTROL_PLANE_K8S_HELM_TIMEOUT`. `/health/platform` reports these values so operators can confirm the active workload deployment posture without shelling into the worker.

Reconciliation is Helm-aware when persisted Helm release metadata is available: running deployments are checked with `helm status`, and the newest failed or stopped deployment may remove a genuinely leftover release with `helm uninstall`. Historical deployment records never clean a release referenced by a newer attempt. Kubernetes failure diagnostics still use the existing direct resource and pod inspection behavior.

Only the worker and reconciler mount the kubeconfig Secret when configured; the API does not need Kubernetes credentials. Consequently, `/health/platform` reports API configuration posture rather than proving that the API Pod can read a kubeconfig. A shared PersistentVolumeClaim is used so the API can read runtime logs and diagnostics written by the worker.

That tooling is now bundled into the image, but the Docker-socket dependency and shared-filesystem dependency remain known hardening gaps.

## Required Configuration By Mode

### Common

Required in all meaningful deployments:

- `CONTROL_PLANE_DATABASE_URL`
- `CONTROL_PLANE_ENV`
- bearer API tokens, unless `CONTROL_PLANE_ALLOW_AUTH_DISABLED=true` is explicitly used for a local development/test run

Recommended for secured deployments:

- `CONTROL_PLANE_API_TOKEN_READ_ONLY`
- `CONTROL_PLANE_API_TOKEN_DEPLOYER`
- `CONTROL_PLANE_API_TOKEN_ADMIN`
- `CONTROL_PLANE_GITHUB_WEBHOOK_SECRET`

Local development/test only:

- `CONTROL_PLANE_ALLOW_AUTH_DISABLED=true`

### `fake`

Useful for evaluation/demo only:

- no registry required
- no cluster access required

### `local-docker`

Requires:

- Docker CLI in the image
- Docker socket access to a trusted local Docker daemon
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
- `CONTROL_PLANE_K8S_HELM_CHART_PATH` reachable from the worker when Helm mode is enabled

## Local Kubernetes Demo Runbook

This is the recommended portfolio demo path for running the control plane locally while deploying workloads into a local MicroK8s cluster. It intentionally keeps the platform local instead of exposing it publicly, while still demonstrating realistic DevOps surfaces: private Git auth, Docker builds, registry push, Kubernetes deploys, pull secrets, diagnostics, logs, and failure recovery.

After completing this setup, use the [Manual MicroK8s Validation](microk8s-manual-validation.md) checklist to record a repeatable end-to-end validation run without duplicating operational setup or troubleshooting notes.

### Architecture

The local demo has four boundaries:

- the control-plane API, worker, reconciler, and MySQL run through Docker Compose
- the worker builds workload images through the host Docker socket; this is a trusted local boundary, not workload isolation
- the worker pushes workload images to Docker Hub or another registry
- MicroK8s pulls the pushed image and runs the workload pod

For local Compose, the MicroK8s kubeconfig cannot be mounted as-is when it points at `https://127.0.0.1:16443`. Inside the worker container, `127.0.0.1` is the worker container, not the host. The Makefile generates `.runtime/kubeconfig.compose`, rewrites the API server to `https://host.docker.internal:16443`, and adds `tls-server-name: kubernetes` so TLS verification still matches the MicroK8s certificate.

### One-Time Host Setup

MicroK8s should be running and reachable from the host:

```bash
microk8s status --wait-ready
microk8s kubectl get nodes
```

Create a Kubernetes image pull secret in the same namespace used by the control plane:

```bash
microk8s kubectl create secret docker-registry dockerhub-pull \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<dockerhub-username> \
  --docker-password=<dockerhub-token> \
  --namespace default
```

If the secret already exists, delete and recreate it when the Docker Hub token changes:

```bash
microk8s kubectl delete secret dockerhub-pull --namespace default
```

### Local Profile Settings

Create ignored local profiles:

```bash
make env-init
```

For `.env.local-kubernetes`, use this shape:

```env
CONTROL_PLANE_EXECUTOR=kubernetes
CONTROL_PLANE_REGISTRY_ENABLED=true
CONTROL_PLANE_REGISTRY_URL=docker.io
CONTROL_PLANE_REGISTRY_NAMESPACE=<dockerhub-namespace>
CONTROL_PLANE_KUBECONFIG_HOST=/var/snap/microk8s/current/credentials/client.config
CONTROL_PLANE_KUBECONFIG=/tmp/paas-kubeconfig
CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER=https://host.docker.internal:16443
CONTROL_PLANE_K8S_NAMESPACE=default
CONTROL_PLANE_K8S_IMAGE_PULL_SECRET=dockerhub-pull
CONTROL_PLANE_K8S_DEPLOYMENT_MODE=manifest
CONTROL_PLANE_K8S_INGRESS_ENABLED=true
CONTROL_PLANE_K8S_INGRESS_CLASS_NAME=
CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN=127.0.0.1.nip.io
```

For `.env.secrets`, keep tokens and passwords only there:

```env
CONTROL_PLANE_REGISTRY_USERNAME=<dockerhub-username>
CONTROL_PLANE_REGISTRY_PASSWORD=<dockerhub-token>
CONTROL_PLANE_GIT_TOKEN_GITHUB=<github-token>
```

The Makefile loads the selected profile and `.env.secrets` as interpolation inputs, so manual `set -a` or ad hoc exports should not be needed. Compose injects the registry credentials only into the worker and the documented Git token into API and worker.

### Local Workload Ingress

Enable the MicroK8s ingress addon once on the host:

```bash
microk8s enable ingress
microk8s kubectl get ingressclass
```

Leave `CONTROL_PLANE_K8S_INGRESS_CLASS_NAME` empty when the cluster has a default class. If no class is marked as default, set this variable to the class shown by the second command and recreate the worker. The local profile uses `127.0.0.1.nip.io`, so generated workload hosts resolve to the loopback address without editing `/etc/hosts`. Deployment IDs use an alphabetic encoding in public hostnames (for example, `paas-deployment-q.127.0.0.1.nip.io`) because numeric labels before the dotted IP can make `nip.io` resolve the wrong address.

New deployments create an Ingress and expose a browser URL in the UI. Existing deployments must be redeployed. Validate a generated host with:

```bash
microk8s kubectl get ingress --namespace default
curl -v http://<generated-host>/
```

The worker healthcheck continues to use a temporary Service port-forward, so rollout validation remains independent from external DNS and ingress-controller readiness.

When MicroK8s runs inside WSL2 and the browser runs on Windows, `127.0.0.1` refers to different network namespaces. Detect the current WSL address with:

```bash
make wsl-ingress-domain
```

Copy the printed assignment into `.env.local-kubernetes`, recreate the runtime, and deploy again. WSL addresses may change after a Windows restart. `make k8s-demo-check PROFILE=local-kubernetes` validates the Ingress API, configured class, DNS resolution, and the newest Ingress URL when one exists.

The deployment summary's live-health indicator checks the public URL from the control-plane API environment. It cannot establish whether a Windows browser can reach the WSL address. Use **Open service in browser** after configuring the WSL ingress domain to verify that client path. Manifest resources belong to one deployment attempt and may be cleaned from its historical record. Helm uses one shared release per project and environment: select the newest deployment only when intentionally removing the current workload. Historical Helm stop or cleanup commands are recorded as skipped when a newer deployment owns that release.

Kubernetes failure diagnostics expose pod phase, container reason, restart count, workload images, and imagePullSecrets as structured fields. **Copy bundle** and **Download bundle** export the currently loaded summary, diagnostics, events, build-log tail, and runtime-log tail as redacted JSON suitable for troubleshooting or a portfolio walkthrough.

### Start And Validate The Runtime

For the first start, build and launch the complete Compose stack, including MySQL, migrations, API, worker, and reconciler:

```bash
make compose-up PROFILE=local-kubernetes
```

This command stays attached so the component logs remain visible. Run the validation commands below from another terminal. After the complete stack has been started once, use `make compose-recreate-runtime PROFILE=local-kubernetes` to rebuild and recreate only the worker and reconciler after executor configuration changes.

Validate worker tooling:

```bash
make compose-toolcheck PROFILE=local-kubernetes
```

Run the full local Kubernetes demo readiness check:

```bash
make k8s-demo-check PROFILE=local-kubernetes
```

This verifies worker tools, BuildKit/buildx, kubeconfig readability, Kubernetes API readiness, `CONTROL_PLANE_K8S_IMAGE_PULL_SECRET`, and the referenced pull secret in the target namespace.

Validate Kubernetes API access from inside the worker:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" get --raw=/readyz'
```

Expected output:

```text
ok
```

Check platform readiness from the API:

```bash
make health-platform PROFILE=local-kubernetes
```

### UI Project Settings

For a private GitHub repository:

- repo URL: `https://github.com/<owner>/<repo>.git`
- branch: `main` or the branch being demonstrated
- Git auth type: `token`
- Git secret ref: `GITHUB`

For a Docker Hub-backed Kubernetes workload:

- deployment target: Kubernetes executor through the active platform profile
- runtime port: the port the application listens on inside the container, for example `5000`
- healthcheck path: a path that returns HTTP 2xx, for example `/health`
- environment variables: application-specific variables, not control-plane variables

For a generic demo workload, use application-owned environment variables such as:

```text
APP_ENV=demo
FEATURE_MESSAGE=Running through the local PaaS
DEMO_HEALTH_STATUS=healthy
```

Do not pass `CONTROL_PLANE_*` values to the workload unless the workload explicitly expects them. Those variables configure the platform, not the deployed application.

`APP_VERSION` and `APP_COMMIT_SHA` are reserved workload identity variables. The worker injects the unique build tag and exact build commit SHA immediately before deployment, replacing project entries with those names.

### Expected Successful Path

A successful Kubernetes demo deployment should show this sequence in the deployment history:

- Git commit resolved
- repository cloned
- Docker image built with BuildKit
- image pushed to the registry
- image verified with `docker buildx imagetools inspect`
- Kubernetes preflight checks passed
- manifest applied
- rollout succeeded
- healthcheck succeeded
- deployment marked `running`

From the cluster side:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" get pods,svc -l app.kubernetes.io/managed-by=autodeploy-control-plane'
```

The active workload pod should be `Running` and `READY 1/1`.

### Cleanup

Old failed deployments can be left in the UI history for auditability. To remove runtime resources for a specific Kubernetes deployment while preserving build, event, audit, and diagnostic history, use **Cleanup Kubernetes resources** in the UI or call the cleanup endpoint:

```bash
curl -X POST http://127.0.0.1:5000/api/projects/<project-id>/deployments/<deployment-id>/cleanup \
  -H "Authorization: Bearer ${CONTROL_PLANE_API_TOKEN_DEPLOYER}" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Demo cleanup"}'
```

Load `CONTROL_PLANE_API_TOKEN_DEPLOYER` from the trusted local `.env.secrets` file before running this command. The Authorization header may be omitted only when the selected local development profile explicitly uses `CONTROL_PLANE_ALLOW_AUTH_DISABLED=true`.

The cleanup action removes the managed Deployment, Service, and Ingress in manifest mode. In Helm mode it uninstalls the stable project/environment release, not an attempt-specific revision. Therefore, issue an explicit Helm stop or cleanup only for the newest deployment of that project and environment and only when the workload itself should be removed. Commands against a historical Helm deployment are skipped when a newer record owns the release; historical records remain in the UI without consuming separate Kubernetes resources. Use direct `kubectl delete` only as a break-glass fallback after identifying the exact stale resource names:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" delete deployment/<name> service/<name> ingress/<name> --ignore-not-found=true'
```

## Troubleshooting

Numeric runtime settings are validated at application startup. Timeouts, polling intervals, content length, and claim TTL must be positive; command retry count and claim refresh interval cannot be negative; claim refresh must remain lower than claim TTL. Invalid values stop startup with the setting name instead of failing later inside a worker loop.

### API is up but `/health/db` fails

The response intentionally does not include raw database driver errors. Check API logs for the internal exception details, then verify:

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
- the repository, Dockerfile, test command, and workload image are trusted for this local Docker socket mode

### `kubernetes` deployments fail before apply

Check:

- `/health/platform`
- registry push configuration
- the kubeconfig Secret is mounted and `CONTROL_PLANE_KUBECONFIG` points at that file
- referenced ConfigMaps and Secrets exist in the target namespace

If the failure says the worker cannot find `kubectl`, remember that the worker executes a binary named `kubectl`. A shell alias such as `alias kubectl='microk8s kubectl'` is not visible to the worker. For local MicroK8s, create a wrapper:

```bash
sudo tee /usr/local/bin/kubectl >/dev/null <<'EOF'
#!/bin/sh
exec /snap/bin/microk8s kubectl "$@"
EOF
sudo chmod +x /usr/local/bin/kubectl
```

Then verify as the same user that runs the API and worker:

```bash
kubectl get nodes
kubectl get secret dockerhub-pull -n default
```

If MicroK8s returns `access denied`, add the user to the `microk8s` group and open a fresh shell:

```bash
sudo usermod -a -G microk8s "$USER"
newgrp microk8s
```

### Kubernetes rollout times out while the Pod later becomes ready

`CONTROL_PLANE_K8S_ROLLOUT_TIMEOUT_SECONDS` controls the primary `kubectl rollout status` wait and defaults to 120 seconds. It is separate from `CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS`, which controls the application HTTP healthcheck. Local or cold clusters can need most of a minute before the Deployment controller observes a new generation even when the image is already present.

When Kubernetes reports `timed out waiting for the condition`, the worker performs one additional 15-second rollout check before recording failure. If that grace check also fails, inspect the persisted rollout diagnostics and Pod events for scheduling, image-pull, crash, or readiness failures rather than increasing the timeout indefinitely.

The worker persists `deploy_target=kubernetes` before preflight and apply. Therefore a partial apply that eventually fails remains eligible for diagnostics, explicit cleanup, and reconciliation of leftover Deployment, Service, Ingress, or Helm resources.

### Docker build fails with `the --mount option requires BuildKit`

This means a Dockerfile uses BuildKit-only syntax such as:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/pip pip install --require-hashes -r requirements.lock.txt
```

Check from inside the worker:

```bash
make compose-toolcheck PROFILE=local-kubernetes
```

The worker should have:

- `docker`
- `docker buildx`
- `DOCKER_BUILDKIT=1`

Then rebuild the runtime:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
```

### `stat /path/to/kubeconfig: no such file or directory`

This means the placeholder kubeconfig path was used. In Compose mode there are two paths:

- `CONTROL_PLANE_KUBECONFIG_HOST`: path on the host, for example `/var/snap/microk8s/current/credentials/client.config`
- `CONTROL_PLANE_KUBECONFIG`: path inside the container, normally `/tmp/paas-kubeconfig`

After changing either value:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
make compose-toolcheck PROFILE=local-kubernetes
```

### `/tmp/paas-kubeconfig: Permission denied`

The kubeconfig file is mounted, but the worker user cannot read it. The Compose Makefile target calculates the file group and adds it to the worker and reconciler containers.

Recreate the runtime so container group membership is applied:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
make compose-toolcheck PROFILE=local-kubernetes
```

### `connect: connection refused` to `https://127.0.0.1:16443`

This means the worker is using a host kubeconfig from inside a container. In the worker container, `127.0.0.1` points to the worker container, not the MicroK8s host.

Use the Makefile-managed Compose kubeconfig flow:

```env
CONTROL_PLANE_KUBECONFIG_HOST=/var/snap/microk8s/current/credentials/client.config
CONTROL_PLANE_KUBECONFIG=/tmp/paas-kubeconfig
CONTROL_PLANE_KUBECONFIG_CONTAINER_SERVER=https://host.docker.internal:16443
```

Then:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
```

The generated `.runtime/kubeconfig.compose` is ignored by Git and rewrites the server endpoint for container access.

### TLS fails for `host.docker.internal`

If `kubectl` reports that the certificate is valid for `kubernetes` but not `host.docker.internal`, regenerate the Compose kubeconfig:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
```

The generated kubeconfig adds:

```yaml
tls-server-name: kubernetes
```

Validate with:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" get --raw=/readyz'
```

Expected output:

```text
ok
```

### `kubernetes` pods cannot pull private Docker Hub images

Check:

- the pushed `image_ref` in the deployment summary
- the deployment event stream includes `image.verify_succeeded`
- the Docker Hub repository exists under `CONTROL_PLANE_REGISTRY_NAMESPACE`
- `CONTROL_PLANE_K8S_IMAGE_PULL_SECRET` is set before the deployment is created
- the secret exists in the same namespace as the workload

The worker verifies pushed registry images with `docker buildx imagetools inspect` before deployment. This uses the same Docker credential store as the authenticated push and supports private registry images. If `image.verify_failed` appears, fix registry/tag/auth state before investigating Kubernetes pod pull behavior.

On Docker Hub free plans, a repository shown as locked can reject push token scope even when `docker login` succeeds. For the portfolio demo, pre-create the workload repository as public under the configured namespace or use a registry plan/provider that permits the intended private repository. A successful login does not override repository plan restrictions.

Create or replace a Docker Hub pull secret with:

```bash
kubectl create secret docker-registry dockerhub-pull \
  --docker-server=https://index.docker.io/v1/ \
  --docker-username=<dockerhub-username> \
  --docker-password=<dockerhub-token> \
  --namespace default
```

Test private image pull through Kubernetes, not with `ctr`, because `ctr` does not automatically use Kubernetes `imagePullSecrets`:

```bash
kubectl run pull-test \
  --image=docker.io/<namespace>/<repository>:<tag> \
  --restart=Never \
  --namespace default \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"dockerhub-pull"}]}}'

kubectl describe pod pull-test -n default
```

If the deployment event says `kubernetes.rollout_failed` and the pod is `ErrImagePull` or `ImagePullBackOff`, verify the manifest includes the pull secret:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" get deployment <deployment-name> -o jsonpath="{.spec.template.spec.imagePullSecrets[*].name}"; echo'
```

If the output is empty, set:

```env
CONTROL_PLANE_K8S_IMAGE_PULL_SECRET=dockerhub-pull
```

Then recreate the runtime and start a new deployment:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
```

### Kubernetes deploy succeeds far enough to create a pod, but the pod restarts

Check application logs first:

```bash
kubectl logs pod/<pod-name> -n default -c app --tail=100
kubectl logs pod/<pod-name> -n default -c app --previous --tail=100
kubectl describe pod <pod-name> -n default
```

Common causes:

- the app listens on a different port than the project `port`
- the app listens on `127.0.0.1` instead of `0.0.0.0`
- the project `healthcheck_path` does not return HTTP 200
- required app environment variables are missing

For a workload exposing `/health`, a normal demo configuration might be:

```text
APP_ENV=demo
FEATURE_MESSAGE=Running through the local PaaS
DEMO_STARTUP_DELAY_SECONDS=0
DEMO_FAIL_STARTUP=false
DEMO_HEALTH_STATUS=healthy
DEMO_RESPONSE_DELAY_MS=0
```

Controlled demonstrations change one value and redeploy:

- slow rollout: `DEMO_STARTUP_DELAY_SECONDS=8`
- healthcheck failure: `DEMO_HEALTH_STATUS=failed`
- CrashLoopBackOff: `DEMO_FAIL_STARTUP=true`
- visible config update: change `FEATURE_MESSAGE`

Restore the healthy defaults and redeploy for recovery. `DEMO_SECRET` should use a Kubernetes `secret_key_ref`; the workload reports only whether the secret is mounted.

If the UI event is `kubernetes.healthcheck_failed` but the pod says `CrashLoopBackOff`, treat it as an application boot failure first. Read `kubectl logs --previous`; the control-plane healthcheck failed because there was no healthy application process to probe.

### Helm fails before the Service healthcheck

When `helm upgrade --install --wait` fails, inspect Diagnostics for both the original Helm error and its persisted Kubernetes snapshot. Container reason/restart count and **Current logs** / **Previous logs** can explain a generic readiness timeout. **Pod descriptions and events** and the Deployment/Service descriptions provide additional context. A timeout alone does not establish CrashLoopBackOff.

The snapshot is collected by the worker, not by refreshing the UI. Diagnostics shows `diagnostics_snapshot_at` for the stored evidence; older records can report no capture time. Its status is `complete`, `partial`, or `unavailable`; a partial result lists safe collection errors such as missing previous logs, denied access, or an exhausted budget. Fix permissions or use scoped host-side `kubectl` checks if evidence is unavailable. Collection does not replace the original Helm failure or change it into a healthcheck failure.

Failure log excerpts are stored in event metadata and exported by **Copy bundle** / **Download bundle**, even if the Runtime log panel is empty. Detailed workspace files can be removed by existing reconciliation/retention; the persisted excerpts remain. Demonstrate startup failure and recovery with a dedicated project so validation does not affect another workload.

### Kubernetes diagnostics endpoint says the deployment is not Kubernetes

Use the deployment row ID, not the Helm release suffix. Log paths include both IDs:

```text
/tmp/paas-workspaces/project-13/deployment-33/logs/...
```

The matching diagnostics request is:

```bash
curl http://127.0.0.1:5000/api/projects/13/deployments/33/kubernetes-diagnostics
```

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
- stop a deployment through the dedicated stop action
- clean up old Kubernetes runtime resources while preserving control-plane history

Use retry when the selected deployment's persisted commit and configuration must be repeated. Retry fails with `409` if its historical snapshot cannot be validated. Use redeploy when project edits, the current project default test command, and the branch's current head should apply. Summary labels the result as `Historical snapshot` or `Current project`; both actions create a new history record.

Current deployment-platform tradeoffs:

- there is no full release orchestration or rollout-history manager yet
- Kubernetes cleanup preserves control-plane history and removes the managed Deployment, Service, and Ingress or Helm release
- database schema rollback is not automated in the deployment story
- project environment entries marked `is_secret: true` but supplied as literal values are plaintext in the project row, deployment snapshots, and database backups; masking on API reads is not encryption. This is accepted only for trusted local/portfolio use. Use `secret_key_ref` or an external secret workflow for real credentials

For platform rollout rollback:

- revert the image tag in Helm or Compose
- rerun migrations only when the target schema is compatible
- prefer forward-fix migrations over frequent down-migration workflows at this project stage

## Future Hardening

- replace Docker-socket-dependent build execution with a dedicated builder pattern
- add richer execution telemetry for scheduled reconciler jobs
- add chart-level support for an externally managed runtime ConfigMap; `secrets.existingSecret` already supports an externally managed runtime Secret
- support managed/external database documentation more deeply
- add image signing, scanning, and supply-chain metadata once the deployment story grows beyond evaluation/demo scope


## Registry digest verification

For a new registry-backed deployment, inspect the push and verification events before rollout. The push event records the publishing tag and reported digest; the Summary deployment image becomes `repository@sha256:...`. If the push reports no valid digest, the worker fails the push step. If the registry cannot serve that digest, verification fails and rollout does not start. Resolve registry connectivity, credentials, or missing artifact availability and create a new deployment; do not substitute a mutable tag to bypass verification.

Historical tag-based deployments are not converted automatically. Registry-disabled local execution has no registry digest guarantee. Validate tag changes only with a dedicated image and project so another workload's images cannot be affected.
