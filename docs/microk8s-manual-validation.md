# Manual MicroK8s Validation

This checklist validates the two Kubernetes boundaries in the repository:

1. the AutoDeploy control plane running on MicroK8s through its internal Helm chart;
2. a user workload deployed by the control-plane Kubernetes executor in Helm mode.

This is a manual validation procedure, not the canonical setup guide. Complete the host, registry, kubeconfig, ingress, and troubleshooting steps in the [Operations Runbook](runbook.md) first.

## Validation Record

Record the environment for each run instead of assuming versions from a previous workstation:

```text
Date:
Operator:
Git commit:
Operating system / WSL version:
Docker version:
MicroK8s version:
Kubernetes version:
Helm version:
Result: PASS / FAIL
Notes:
```

Do not record tokens, passwords, kubeconfig contents, or Kubernetes Secret values.

## Common Prerequisites

- MicroK8s reports ready.
- The local registry addon is enabled and reachable at `localhost:32000`.
- Docker can push to the registry.
- Helm and MicroK8s are reachable from the host.
- The control-plane image contains Git, Docker/buildx, kubectl, and Helm.
- Repository environment profiles and secrets have been initialized with `make env-init`.

Capture the tool versions and basic readiness:

```bash
git rev-parse HEAD
docker version
microk8s version
microk8s status --wait-ready
microk8s kubectl version
microk8s kubectl get nodes
helm version
curl -fsS http://localhost:32000/v2/_catalog
```

Expected:

- every command exits with `0`;
- the MicroK8s node reports `Ready`;
- the registry returns a JSON catalog.

## Scenario A — Control Plane Installed With Helm

This scenario validates the platform chart itself. The local values file uses the fake executor, a hostPath workspace, SQLite, and explicit Docker socket access for a trusted single-node demo.

### A1. Validate deployment assets

```bash
helm lint ./deploy/helm/autodeploy-control-plane
helm template ci ./deploy/helm/autodeploy-control-plane \
  -f ./deploy/helm/autodeploy-control-plane/values.ci.yaml >/dev/null
helm template local ./deploy/helm/autodeploy-control-plane \
  -f ./deploy/helm/autodeploy-control-plane/values.local-microk8s.yaml >/dev/null
```

Expected: both templates render and lint succeeds.

### A2. Build and publish the control-plane image

```bash
docker build -t localhost:32000/autodeploy-control-plane:dev .
docker run --rm --entrypoint helm localhost:32000/autodeploy-control-plane:dev version --short
docker push localhost:32000/autodeploy-control-plane:dev
```

Expected:

- the image builds successfully;
- Helm runs inside the final runtime image;
- the image is pushed to the local registry.

### A3. Install or upgrade the platform

```bash
helm upgrade --install local ./deploy/helm/autodeploy-control-plane \
  --namespace paas-local \
  --create-namespace \
  -f ./deploy/helm/autodeploy-control-plane/values.local-microk8s.yaml \
  --set image.tag=dev \
  --wait \
  --timeout 240s
```

Inspect the result:

```bash
microk8s kubectl get pods,deployments,services,jobs,cronjobs --namespace paas-local
microk8s kubectl get pods --namespace paas-local -o wide
```

Expected:

- migration Job completed;
- API Deployment is available;
- worker Deployment is available;
- reconciler CronJob exists;
- running containers use `localhost:32000/autodeploy-control-plane:dev`.

### A4. Validate the API

Start a temporary port-forward:

```bash
microk8s kubectl port-forward \
  --namespace paas-local \
  service/local-autodeploy-control-plane \
  18080:5000
```

From another terminal:

```bash
curl -fsS http://127.0.0.1:18080/health
curl -fsS http://127.0.0.1:18080/health/platform
```

Expected: `/health` returns `status: ok`, and platform health reports the configured fake executor without exposing secrets.

## Scenario B — Helm-Managed User Workload

This scenario validates the real Kubernetes executor with `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`. Use an existing small Dockerfile-based demo repository with a known port and an HTTP health endpoint. Do not create an ad hoc application inside this document.

The recommended local topology is the Compose control plane connected to MicroK8s, as documented in the runbook.

### B1. Configure the local Kubernetes profile

Ensure `.env.local-kubernetes` contains the equivalent of:

```env
CONTROL_PLANE_EXECUTOR=kubernetes
CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED=true
CONTROL_PLANE_REGISTRY_ENABLED=true
CONTROL_PLANE_REGISTRY_URL=<registry-host>
CONTROL_PLANE_REGISTRY_NAMESPACE=<registry-namespace>
CONTROL_PLANE_KUBECONFIG_HOST=/var/snap/microk8s/current/credentials/client.config
CONTROL_PLANE_KUBECONFIG=/tmp/paas-kubeconfig
CONTROL_PLANE_K8S_NAMESPACE=default
CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm
CONTROL_PLANE_K8S_HELM_CHART_PATH=deploy/helm/generic-web-app
```

Keep registry credentials, API tokens, and Git tokens only in `.env.secrets`.

The executor passes `CONTROL_PLANE_KUBECONFIG` explicitly to kubectl and Helm. `KUBECONFIG` is needed only for manual host-side Helm commands.

### B2. Start and validate the control plane

```bash
make compose-up PROFILE=local-kubernetes
```

In another terminal:

```bash
make compose-toolcheck PROFILE=local-kubernetes
make k8s-demo-check PROFILE=local-kubernetes
make health-platform PROFILE=local-kubernetes
```

Expected:

- Git, Docker/buildx, kubectl, and Helm are available inside the worker;
- the Docker socket and kubeconfig are readable;
- Kubernetes API and registry prerequisites pass;
- platform health reports `executor: kubernetes` and `deployment_mode: helm`.

### B3. Create the demo project and trigger deployment

Create the project through the UI or `POST /api/projects` using:

- a real Git repository URL or an allowed development-only local repository;
- the repository branch;
- Dockerfile and build-context paths;
- the container port;
- an HTTP healthcheck path;
- registry and Git Secret references when needed.

Trigger the deployment through the UI or:

```text
POST /api/projects/<project-id>/deploy
```

When API authentication is enabled, use a deployer or admin bearer token. Do not paste the token into this document or validation record.

### B4. Verify worker and Helm behavior

Wait for the background worker, then inspect:

```text
GET /api/projects/<project-id>/deployments/latest
GET /api/projects/<project-id>/deployments/<deployment-id>/events
GET /api/projects/<project-id>/deployments/<deployment-id>/summary
```

Expected event sequence includes:

- `repository.clone_succeeded`;
- `image.build_succeeded`;
- `image.push_succeeded`;
- `image.verify_succeeded`;
- `deployment.preflight_succeeded`;
- `kubernetes.helm_deploy_started`;
- `kubernetes.helm_deploy_succeeded`;
- `kubernetes.healthcheck_succeeded`;
- `deployment.running`.

Expected summary fields include:

- `deploy_target: kubernetes`;
- `deployment_mode: helm` in event metadata;
- persisted Helm release name, namespace, and chart path;
- healthcheck and Pod diagnostic fields.

Verify the cluster resources using the persisted release name from the summary:

```bash
export KUBECONFIG=/var/snap/microk8s/current/credentials/client.config
helm status <release-name> --namespace default
microk8s kubectl get deployment,service,pod \
  --namespace default \
  -l app.kubernetes.io/instance=<release-name>
```

Expected: the release is deployed and the workload Pod is ready.

### B5. Validate asynchronous stop

Request stop through the dedicated endpoint:

```text
POST /api/projects/<project-id>/deployments/<deployment-id>/stop
```

Expected immediate result: HTTP `202` with a pending or existing `DeploymentCommand`. The HTTP request does not execute Helm directly.

Allow the Compose worker loop to process the command, then poll the deployment and events. For a host-run one-shot worker, run:

```bash
.venv/bin/python -m flask --app wsgi:app run-worker-once
```

Expected terminal events:

- `deployment.stop_requested`;
- `deployment.stop_started`;
- `kubernetes.helm_uninstall_started`;
- `kubernetes.helm_uninstall_succeeded`;
- `deployment.stopped`.

Verify removal:

```bash
helm status <release-name> --namespace default
microk8s kubectl get all \
  --namespace default \
  -l app.kubernetes.io/instance=<release-name>
```

Expected: Helm reports the release absent and no workload resources remain.

## Failure Evidence

If a check fails, record only bounded, redacted evidence:

- failing check identifier;
- command exit code;
- deployment ID and event type;
- sanitized error summary;
- relevant Pod phase/container reason;
- link to the applicable [Runbook troubleshooting section](runbook.md#troubleshooting).

Never copy `.env.secrets`, bearer tokens, registry passwords, kubeconfig contents, or Kubernetes Secret values into validation notes.

## Completion Checklist

- [ ] Tool versions and Git commit recorded
- [ ] MicroK8s node ready
- [ ] Local registry reachable
- [ ] Control-plane charts lint and render
- [ ] Runtime image contains Helm
- [ ] Migration Job completed
- [ ] API and worker available
- [ ] Reconciler CronJob present
- [ ] Worker toolcheck and Kubernetes readiness pass
- [ ] User workload reaches `running`
- [ ] Helm release and workload Pod are healthy
- [ ] Stop request returns `202`
- [ ] Worker processes the stop command
- [ ] Helm release and workload resources are removed
- [ ] Result and redacted notes recorded
