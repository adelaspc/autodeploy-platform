# Platform Contract

A deployable user application must:

- be hosted in a GitHub repository
- have a valid Dockerfile
- build into a container image
- push successfully to the configured container registry when using the Kubernetes executor
- expose exactly one HTTP port
- provide a healthcheck endpoint
- be stateless
- use environment variables for runtime configuration
- store persistent data externally

Registry publishing is optional for the `local-docker` executor, which can deploy the locally built image directly. The Kubernetes executor requires registry publishing because it deploys the pushed `image_ref`, not the worker's local image tag.

When a deployment record is created, the control plane stores a versioned snapshot of the project settings used by execution: repository and branch, Git auth reference, Dockerfile and build context, port and healthcheck path, environment definitions, resource requests, runtime, and project identity. Editing the project afterward affects future deployments, not work already queued. The worker also checks out the recorded commit SHA explicitly, so a moving branch cannot change the source being built.

The snapshot is immutable evidence; the `PlatformDeployment` row is a mutable lifecycle record. Its status, claims, service/runtime identifiers, command outcomes, and event relationship change as execution progresses, while its persisted snapshot remains the historical input record.

Immediately before creating a workload, the platform resolves two reserved environment variables from the build: `APP_COMMIT_SHA` receives the exact build commit SHA and `APP_VERSION` receives the unique build image tag. Project entries with either name are deliberately overridden. This resolution is shared by fake execution metadata, local Docker `--env` arguments, generated Kubernetes manifests, and generated Helm values.

For records created before snapshot support was introduced, the database migration freezes the best available project configuration at upgrade time; it cannot reconstruct older configuration that was never persisted.

The retry action requires a complete, supported snapshot and reuses the selected build's exact commit and effective test command, including an explicitly disabled command. It rejects unverifiable legacy records instead of silently mixing historical and current inputs. Redeploy is the operator's explicit choice to combine the latest deployment branch with its current head and the current project configuration.

Manifest-mode Kubernetes resources are unique to each deployment attempt. Helm mode instead uses one stable release for each project and environment. A newer deployment owns that shared release, so stop and cleanup commands for historical Helm deployments are recorded as skipped rather than uninstalling the current workload.

The v1 operator workflow creates deployments in the `production` environment. The persisted deployment schema and the admin-only manual-record endpoint also retain an `environment` field so historical records, manual test fixtures, and Helm release identity can distinguish environments. This is data-model support, not a user-facing multi-environment promotion feature.

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

- `deploy/helm/autodeploy-control-plane` deploys internal PaaS platform components.
- `deploy/helm/generic-web-app` deploys stateless user web workloads managed by the PaaS.

The control-plane chart may contain platform-specific API, worker, reconciler, migration, workspace, RBAC, Docker socket, and kubeconfig behavior. Its default RBAC is namespace-scoped and optimized for manifest mode; Helm release Secret mutation is an explicit chart value.

The generic web app chart is stack-agnostic. It renders one Deployment, one Service, and an optional Ingress. It does not assume Python, Node, PHP, migrations, framework commands, persistent storage, RBAC, Docker socket access, or kubeconfig access.

The Kubernetes executor supports two workload deployment modes:

- `manifest`: default; generates Deployment, Service, and optional Ingress manifests directly, then uses `kubectl apply` and `kubectl delete`
- `helm`: generates `generic-web-app` values, installs or upgrades a stable Helm release, and uninstalls that release on stop

The worker freezes the selected mode and namespace on each deployment before preflight or apply. In manifest mode it also freezes DNS-1123-compatible Deployment, Service, and Ingress names, each at most 63 characters. Later stop, cleanup, status, and reconciliation operations use this persisted attempt identity even if Kubernetes configuration or naming rules have changed. Legacy Kubernetes records with Helm release metadata are treated as Helm-managed; legacy records without it are treated as manifest-managed, with namespace and exact resource names recovered from their earliest persisted runtime metadata when possible. The migration uses the historical naming rule as a fallback so existing resources are not orphaned.

When registry push is enabled, the worker verifies the remote image reference with `docker buildx imagetools inspect` before applying workload resources. Push success and remote verification are separate events; a verification failure does not retroactively change a successful registry push result.

Helm mode uses a generated-values contract. The mapper converts the existing project/deployment/build model into generic chart values before the executor calls Helm.

Both Kubernetes modes use the same workload configuration: the `http` container port, optional CPU/memory requests, and readiness/liveness probes. Readiness probes use a 5-second initial delay and 10-second period; liveness probes use a 15-second initial delay and 20-second period. Both request the recorded `healthcheck_path` on the named `http` port. Resource limits and startup probes are not configured by the project model.

Current mapping boundaries:

- `Build.image_ref` is the preferred source for `image.repository` and `image.tag`
- the deployment's project snapshot `port` maps to `container.port` and `service.port`
- literal env vars from the deployment snapshot map to `env[].value`; platform-resolved `APP_COMMIT_SHA` and `APP_VERSION` are appended after project variables and override same-named project entries
- ConfigMap and Secret key references map to `env[].valueFrom`
- whole-resource `envFrom` imports are not generated yet because the model does not expose that concept
- the deployment snapshot `cpu` and `memory` map to optional container `resources.requests` in both Kubernetes modes
- the deployment snapshot `healthcheck_path` maps to readiness and liveness probe paths in both Kubernetes modes
- startup probes remain disabled by default; Ingress follows the platform-wide Kubernetes Ingress configuration
- test and migration commands are not treated as runtime container command or args

Helm-managed workload naming follows this shape:

```text
paas-<project-slug>-<environment-slug>-<project-id-suffix>
```

The convention is one release per project/environment workload, not one release per deployment attempt. Redeploys upgrade the same release, while every attempt remains a separate control-plane history record with an immutable input snapshot. Stop behavior uninstalls the release. Reconciliation uses persisted Helm release metadata for Helm-managed workloads.

Because multiple history records refer to the same stable release, release ownership belongs to the newest deployment for that project and environment. Automatic reconciliation cleanup must not infer ownership from an older failed or stopped record. Before removing a leftover release, the reconciler ends its current read transaction and checks for newer deployment records against a fresh database view. If a newer record exists, the older record is historical and cannot uninstall the shared release. This also prevents a reconciliation pass that began before a redeployment was created from deleting the newly upgraded workload under MySQL repeatable-read isolation.

An isolated Helm runner abstraction exists for the Helm-mode path. The Kubernetes executor can now use it for deploy and stop when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`.

Current deployment modes:

- `manifest`: default; keeps the existing direct manifest generation, `kubectl apply`, and `kubectl delete` behavior
- `helm`: uses generated generic chart values, stable release naming, `HelmRunner.upgrade_install(...)` for deploy, and `HelmRunner.uninstall(...)` for stop

Helm mode treats release-not-found uninstall failures as idempotently stopped. Reconciliation uses `helm status` for running Helm releases. Only the newest deployment record for a project/environment workload may use `helm uninstall` to remove a leftover release; older records remain available for history and diagnostics without owning runtime cleanup. Kubernetes diagnostics still use direct resource and pod inspection.

Helm-mode flow:

```text
project/build/deployment
  -> generated generic-web-app values
  -> stable release name
  -> HelmRunner.upgrade_install(...)
  -> one Helm release per project/environment
```
