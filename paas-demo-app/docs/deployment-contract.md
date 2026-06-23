# Platform Contract

A deployable user application must:

- be hosted in a GitHub repository
- have a valid Dockerfile
- build into a container image
- push successfully to the configured container registry
- expose exactly one HTTP port
- provide a healthcheck endpoint
- be stateless
- use environment variables for runtime configuration
- store persistent data externally

## Development-Only Exception

For local development and integration testing, the control plane may accept a local filesystem repository path as `repo_url` when `CONTROL_PLANE_ENV=development`.

Outside development, project create and update requests must use supported remote Git repository URLs. Local filesystem paths are rejected in production-like environments.

At this stage, supported remote Git repositories are limited to canonical GitHub HTTPS URLs:

- `https://github.com/<owner>/<repo>`
- `https://github.com/<owner>/<repo>.git`

The control plane normalizes accepted GitHub repository URLs to `https://github.com/<owner>/<repo>.git` for storage and matching.

Private repositories are supported via GitHub HTTPS plus token auth. SSH Git authentication is not supported yet.

## Platform-Owned Workers

The platform is allowed and expected to run its own internal workers.

Examples:

- build worker
- deployment worker
- queue worker for deployment jobs
- healthcheck worker

These are platform infrastructure components, not user application workloads.

## User Application Workloads

In v1, a user application may only define one web workload.

User-managed background workers, queue consumers, scheduled jobs, and multi-process workloads are not supported in v1.

## Helm Chart Boundary

The repository has two distinct Helm chart responsibilities:

- `deploy/helm/paas-control-plane` deploys internal PaaS platform components.
- `deploy/helm/generic-web-app` deploys stateless user web workloads managed by the PaaS.

The control-plane chart may contain platform-specific API, worker, reconciler, migration, workspace, RBAC, Docker socket, and kubeconfig behavior.

The generic web app chart is stack-agnostic. It renders one Deployment, one Service, and an optional Ingress. It does not assume Python, Node, PHP, migrations, framework commands, persistent storage, RBAC, Docker socket access, or kubeconfig access.

The Kubernetes executor supports two workload deployment modes:

- `manifest`: default; generates minimal Deployment and Service manifests directly, then uses `kubectl apply` and `kubectl delete`
- `helm`: generates `generic-web-app` values, installs or upgrades a stable Helm release, and uninstalls that release on stop

Helm mode uses a generated-values contract. The mapper converts the existing project/deployment/build model into generic chart values before the executor calls Helm.

Current mapping boundaries:

- `Build.image_ref` is the preferred source for `image.repository` and `image.tag`
- `Project.port` maps to `container.port` and `service.port`
- literal project env vars map to `env[].value`
- ConfigMap and Secret key references map to `env[].valueFrom`
- whole-resource `envFrom` imports are not generated yet because the model does not expose that concept
- `Project.healthcheck_path` maps to readiness and liveness probe paths for Helm-managed workloads
- startup probes and ingress remain disabled by default
- test and migration commands are not treated as runtime container command or args

Helm-managed workload naming follows this shape:

```text
paas-<project-slug>-<environment-slug>-<project-id-suffix>
```

The convention is one release per project/environment workload, not one release per deployment attempt. Redeploys upgrade the same release. Stop behavior uninstalls the release. Reconciliation uses persisted Helm release metadata for Helm-managed workloads.

An isolated Helm runner abstraction exists for the Helm-mode path. The Kubernetes executor can now use it for deploy and stop when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`.

Current deployment modes:

- `manifest`: default; keeps the existing direct manifest generation, `kubectl apply`, and `kubectl delete` behavior
- `helm`: uses generated generic chart values, stable release naming, `HelmRunner.upgrade_install(...)` for deploy, and `HelmRunner.uninstall(...)` for stop

Helm mode treats release-not-found uninstall failures as idempotently stopped. Reconciliation uses `helm status` for running Helm releases and `helm uninstall` for failed or stopped deployments with leftover releases. Kubernetes diagnostics still use direct resource and pod inspection.

Helm-mode flow:

```text
project/build/deployment
  -> generated generic-web-app values
  -> stable release name
  -> HelmRunner.upgrade_install(...)
  -> one Helm release per project/environment
```
