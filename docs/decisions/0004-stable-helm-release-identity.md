# ADR 0004: Stable Helm Release Identity

**Status:** Accepted

## Context

Every deployment attempt needs an immutable control-plane record, but creating a separate Helm release for every attempt would leave parallel workloads and make ordinary redeployment behave like environment creation. The desired workload identity is project plus environment, not attempt ID.

## Decision

Helm mode uses one stable release name per project and environment. A new deployment record upgrades that release with the new image and specification. Runtime identity is persisted on the deployment record so stop and reconciliation do not have to reconstruct it from current naming rules.

Because older records also refer to the stable release, only the newest deployment for that project/environment may perform automatic reconciliation cleanup. The reconciler checks this ownership using a fresh database view before uninstalling.

## Consequences

- Redeployments replace the managed workload while retaining independent attempt history.
- Helm upgrade, rollback behavior, and release metadata remain available.
- Historical records are not independent owners of runtime resources.
- Cleanup logic must distinguish immutable history from current release ownership.
- Explicit cleanup is authoritative and must be used against the deployment that currently owns the workload.
