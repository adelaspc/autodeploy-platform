# ADR 0001: Database-Backed Asynchronous Work

**Status:** Accepted

## Context

Building images and changing runtime resources can take minutes and can outlive an HTTP request. The platform already requires MySQL for projects, deployments, events, and audit data. Adding a message broker would introduce another operational dependency for a portfolio-scale, single-operator system.

## Decision

The API persists deployment and command records and returns before execution. Workers poll MySQL, claim eligible records with guarded updates, refresh claims during long operations, and verify ownership before important writes. The reconciler expires stale claims and records recovery evidence.

Stop and cleanup requests use the same model through `DeploymentCommand`. An active-command uniqueness key prevents duplicate in-flight commands of the same type.

## Consequences

- The API remains responsive while work runs asynchronously.
- Work state, ownership, and operator evidence share one transactional store.
- Multiple workers can compete safely for eligible records without requiring a broker.
- Polling adds database load and latency compared with push-based delivery.
- MySQL is both the system of record and the work-dispatch dependency.
- External side effects are not part of the database transaction, so processing is not exactly once and requires reconciliation and idempotent operations.
