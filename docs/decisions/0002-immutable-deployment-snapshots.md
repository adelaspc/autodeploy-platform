# ADR 0002: Immutable Deployment Specification Snapshots

**Status:** Accepted

## Context

Projects are mutable, but queued work and historical records must remain explainable. If a worker rereads the current project row, an edit made after a deployment request could change repository settings, commands, environment references, or runtime resources while that request is executing.

## Decision

Creating a deployment stores a versioned snapshot of the execution-relevant project specification and resolves the exact Git commit SHA. Workers execute from this snapshot. Project edits configure future deployments only.

Secret references are included as symbolic references. Git tokens and Kubernetes Secret values are not copied into the snapshot.

## Consequences

- A queued deployment has deterministic inputs even when its project changes.
- Historical events and diagnostics can be interpreted against the configuration that produced them.
- Recovery is represented by a new deployment instead of mutation of the failed record.
- Snapshot schema changes require versioning and migration/backward-compatibility handling.
- Records created before snapshot support can only preserve the best configuration available at migration time.
