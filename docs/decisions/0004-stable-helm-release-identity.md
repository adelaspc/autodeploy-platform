# ADR 0004: Stable Helm Release Identity

**Status:** Accepted;

## Context

Every deployment attempt needs an immutable control-plane record, but creating a separate Helm release for every attempt would leave parallel workloads and make ordinary redeployment behave like environment creation. The desired workload identity is project plus environment, not attempt ID.

## Decision

Helm mode uses one stable release name per project and environment. A new deployment record upgrades that release with the new image and specification.

For new Kubernetes attempts, the worker persists mode and namespace immediately before preflight and the first Kubernetes side effect. Manifest attempts also persist their exact Deployment, Service, and Ingress names at that boundary. A successful Helm result persists release name, namespace, and chart path; when Helm fails before that result can be applied, its earliest event metadata remains the recovery source for the release identity.

Stop and reconciliation resolve this recorded attempt identity before consulting executor defaults. They therefore continue to address the original mode, namespace, and resource/release names after configuration or naming rules change. Migrations backfill legacy rows from Helm columns, preflight data, and the earliest relevant event; legacy manifest names without recorded metadata use the historical naming algorithm. A legacy namespace that was never recorded cannot be recovered and necessarily falls back to the currently configured namespace. This is the explicit limit of the guarantee.

Because older records also refer to the stable release, only the newest deployment for that project/environment may perform automatic reconciliation cleanup. The reconciler checks this ownership using a fresh database view before uninstalling.

## Consequences

- Redeployments replace the managed workload while retaining independent attempt history.
- Helm upgrade, rollback behavior, and release metadata remain available.
- Historical records are not independent owners of runtime resources.
- Cleanup logic must distinguish immutable history from current release ownership.
- Explicit cleanup is authoritative and must be used against the deployment that currently owns the workload.
- Runtime identity is frozen by the worker, not when the API first queues the deployment; configuration changes made while work is still queued can therefore affect the identity selected when execution begins.
- Legacy records retain best-effort recovery semantics when their original namespace was never persisted anywhere.
