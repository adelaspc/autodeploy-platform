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

Each local file is copied from its committed `.example` counterpart. `make env-init` never overwrites an existing local file. Keep runtime configuration in the profile files and all credentials in `.env.secrets`. If local profile files already exist, copy newly added keys such as `CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED` from the matching `.example` file manually.

The Makefile always loads `.env.<profile>` and `.env.secrets` together for runtime commands, so you do not need to manually export variables.

Important demo defaults:

- `CONTROL_PLANE_ENV=development`
- `CONTROL_PLANE_EXECUTOR=fake`
- no real registry, Git, or API secrets required

For private Git repositories, set a token in `.env.secrets` using the project's `git_secret_ref`. For example, a project with `git_secret_ref=GITHUB` uses:

```bash
CONTROL_PLANE_GIT_TOKEN_GITHUB=replace-me
```

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

Protected:

- `GET /health/db`
- `GET /health/platform`
- `GET /health/activity`
- `GET /health/observability`

Operational meaning:

- `/health` verifies the API process is serving requests
- `/health/db` verifies database reachability and returns only a safe status/error code; driver details, paths, hosts, and credentials are kept out of the response
- `/health/platform` shows executor/config readiness
- `/health/activity` gives an operator-oriented platform activity summary
- `/health/observability` reports the safe runtime posture for metrics, logging, and request correlation without exposing secrets

For a selected running deployment with a public service URL, the UI polls its project healthcheck through `GET /api/projects/<id>/deployments/<deployment_id>/live-health`. This live signal is intentionally separate from the persisted deployment lifecycle status: a transient Pod replacement can show `unhealthy` and then recover to `healthy` without rewriting the deployment as failed.

In Compose:

- the API container has a health check against `/health`

In Kubernetes:

- the API deployment uses `/health` for readiness and liveness probes
- the worker deployment uses a minimal exec probe to confirm the long-running process is still alive

## Metrics and Structured Logs

Application logs use JSON on stdout by default. `CONTROL_PLANE_COMPONENT` identifies API, worker, and reconciler records in Docker Compose. Request completion records also contain `request_id`, method, path, status, and duration.

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

For a Helm deployment, set `config.CONTROL_PLANE_METRICS_ENABLED` and provide `secrets.CONTROL_PLANE_METRICS_TOKEN`. The chart does not install Prometheus or create a `ServiceMonitor`.

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

The convention is one release per project/environment workload, not one release per deployment attempt. Redeploys should upgrade the same release. Stop uninstalls the same release in Helm mode. Reconciliation uses the recorded Helm release metadata to verify and clean Helm-managed workloads.

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
- `CONTROL_PLANE_EXECUTOR=fake`, so the local Helm path validates the control-plane runtime itself without deploying workloads
- `dockerSocket.enabled=true`, making the local Docker trust boundary explicit in this local-only override

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

- read-only preflight for referenced ConfigMaps and Secrets
- read-only Pod inspection and Pod logs for diagnostics
- temporary Service port-forward creation for healthchecks
- create/update/patch/delete for managed Services, Deployments, and optional Ingresses
- read-only Events access for operator diagnostics

The default Role is intentionally scoped for manifest mode and does not grant Secret or ConfigMap mutation. Helm mode may need additional Secret mutation permissions because Helm v3 stores release metadata in Secrets by default. Enable that explicitly with:

```yaml
rbac:
  helmReleaseStorage: true
```

`IngressClass` is cluster-scoped, so it is not granted by the namespace Role. When `CONTROL_PLANE_K8S_INGRESS_CLASS_NAME` is set, the configured kubeconfig or ServiceAccount must already be allowed to read the referenced IngressClass, or preflight will report it as missing/unreadable.

Current tradeoff:

- the control-plane image still uses local Docker build/push behavior
- the base chart does not mount `/var/run/docker.sock` by default
- `values.local-microk8s.yaml` enables the Docker socket explicitly for trusted local demos
- Docker socket access is acceptable for a portfolio-grade local demonstration but not a hardened production pattern

For `CONTROL_PLANE_EXECUTOR=kubernetes`, the worker still needs:

- Docker access for build/push when using the current local builder path
- `git`
- `kubectl`
- `helm` when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`
- a host kubeconfig file mounted into the worker container
- `CONTROL_PLANE_KUBECONFIG_HOST` pointing at the host file and `CONTROL_PLANE_KUBECONFIG` pointing at its in-container path
- container group membership for the kubeconfig file group, applied by the Compose Makefile targets
- RBAC in the target namespace

`CONTROL_PLANE_K8S_DEPLOYMENT_MODE` controls the user workload deploy and stop behavior:

- `manifest` is the default and keeps the existing direct `kubectl apply` and `kubectl delete` paths
- `helm` uses generated `generic-web-app` values, a stable release name, `HelmRunner.upgrade_install(...)` for deploy, and `HelmRunner.uninstall(...)` for stop

Helm mode can be tuned with `CONTROL_PLANE_K8S_HELM_CHART_PATH`, `CONTROL_PLANE_K8S_HELM_BINARY`, and `CONTROL_PLANE_K8S_HELM_TIMEOUT`. `/health/platform` reports these values so operators can confirm the active workload deployment posture without shelling into the worker.

Reconciliation is Helm-aware when persisted Helm release metadata is available: running deployments are checked with `helm status`, and leftover releases for failed or stopped deployments are removed with `helm uninstall`. Kubernetes failure diagnostics still use the existing direct resource and pod inspection behavior.

The API deployment also mounts the same kubeconfig secret path when configured so `/health/platform` reports the same readiness posture as the worker. A shared PersistentVolumeClaim is used so the API can read runtime logs and diagnostics written by the worker.

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

The Makefile loads the selected profile and `.env.secrets` together, so manual `set -a` or ad hoc exports should not be needed.

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

The deployment summary checks the public URL from the browser and reports when the browser cannot reach the WSL address. Use **Cleanup Kubernetes resources** on an old deployment to delete its Deployment, Service, and Ingress (or uninstall its Helm release) while preserving build, deployment, event, and audit history.

Kubernetes failure diagnostics expose pod phase, container reason, restart count, workload images, and imagePullSecrets as structured fields. **Copy bundle** and **Download bundle** export the currently loaded summary, diagnostics, events, build-log tail, and runtime-log tail as redacted JSON suitable for troubleshooting or a portfolio walkthrough.

### Start And Validate The Runtime

Rebuild and recreate the runtime containers:

```bash
make compose-recreate-runtime PROFILE=local-kubernetes
```

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

For the Deployment Lab reference workload, use workload env vars like:

```text
APP_ENV=demo
APP_VERSION=1.0.0
APP_COMMIT_SHA=<current-commit>
FEATURE_MESSAGE=Running through the local PaaS
DEMO_HEALTH_STATUS=healthy
```

Do not pass `CONTROL_PLANE_*` values to the workload unless the workload explicitly expects them. Those variables configure the platform, not the deployed application.

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
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" get pods,svc -l app.kubernetes.io/managed-by=paas-control-plane'
```

The active workload pod should be `Running` and `READY 1/1`.

### Cleanup

Old failed deployments can be left in the UI history for auditability. To remove runtime resources for a specific Kubernetes deployment while preserving build, event, audit, and diagnostic history, use **Cleanup Kubernetes resources** in the UI or call the cleanup endpoint:

```bash
curl -X POST http://127.0.0.1:5000/api/projects/<project-id>/deployments/<deployment-id>/cleanup \
  -H 'Content-Type: application/json' \
  -d '{"message":"Demo cleanup"}'
```

The cleanup action removes the managed Deployment, Service, and Ingress in manifest mode, or uninstalls the Helm release in Helm mode. Use direct `kubectl delete` only as a break-glass fallback after identifying the exact stale resource names:

```bash
docker compose exec control-plane-worker sh -lc \
  'kubectl --kubeconfig "$CONTROL_PLANE_KUBECONFIG" --namespace "$CONTROL_PLANE_K8S_NAMESPACE" delete deployment/<name> service/<name> ingress/<name> --ignore-not-found=true'
```

## Troubleshooting

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

### Docker build fails with `the --mount option requires BuildKit`

This means a Dockerfile uses BuildKit-only syntax such as:

```dockerfile
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
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

The worker verifies pushed registry images with `docker buildx imagetools inspect` before deployment. This uses the same Docker credential store as the authenticated push and supports private registry images. If `image.verify.failed` appears, fix registry/tag/auth state before investigating Kubernetes pod pull behavior.

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

For the temporary Deployment Lab reference workload under `demo-app/`, use `healthcheck_path=/health`. A normal demo configuration is:

```text
APP_ENV=demo
APP_VERSION=1.0.0
APP_COMMIT_SHA=<current-commit>
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

Current deployment-platform tradeoffs:

- there is no full release orchestration or rollout-history manager yet
- Kubernetes cleanup preserves control-plane history and removes the managed Deployment, Service, and Ingress or Helm release
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
