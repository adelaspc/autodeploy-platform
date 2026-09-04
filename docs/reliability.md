# Reliability and Recovery

AutoDeploy is designed to make deployment work observable and recoverable on a trusted single-operator platform. It does not claim distributed transactions or exactly-once execution across MySQL, Docker, a registry, Helm, and Kubernetes.

## Reliability Properties

| Concern | Mechanism | Result |
| --- | --- | --- |
| API latency | Deployment and cleanup requests are persisted before execution. | HTTP handlers do not wait for Docker, Helm, or Kubernetes commands. |
| Worker ownership | Pending records are claimed with guarded database updates. | Competing workers do not normally process the same record concurrently. |
| Long-running work | Claims are refreshed during external commands and health polling. | Healthy workers retain ownership beyond the default claim TTL. |
| Lost workers | The reconciler detects stale claims. | Interrupted work becomes an explicit failed record instead of remaining pending forever. |
| Stop/deploy races | Stop is a cooperative cancellation signal before command cleanup takes ownership. | Deployment and command workers avoid conflicting runtime mutations. |
| Repeated cleanup requests | An active-command uniqueness key deduplicates commands by deployment and type. | Operator retries reuse in-flight work while completed commands remain in history. |
| Webhook retries | GitHub delivery IDs are unique. | The same webhook delivery is not converted into duplicate deployment work. |
| Mutable projects | Each deployment stores a versioned specification snapshot and exact commit SHA. | Queued and historical attempts do not drift when the project changes. |
| Helm redeployment | One stable release is used per project and environment. | A redeployment upgrades the workload while creating a new control-plane history record. |
| Automatic Helm cleanup | Only the newest deployment for a project/environment may own automatic release cleanup. | An old reconciled record cannot remove a newer successful workload. |
| Evidence | State transitions, events, command results, log paths, and diagnostics are persisted. | Failures remain inspectable after the worker releases its claim or resources are removed. |

The default claim TTL is 300 seconds and the default refresh interval is 30 seconds. Configuration validation requires the refresh interval to remain below the TTL.

## Processing Guarantees

The database provides a single current owner for a claimed record, not an exactly-once guarantee for external side effects. A worker can successfully run a Docker or Helm command and fail before committing the corresponding database event. Recovery logic therefore relies on idempotent runtime operations where practical and on reconciliation when database state and runtime state diverge.

The important boundary is:

```text
database claim and state transition
        |
        | no shared transaction
        v
Docker / registry / Helm / Kubernetes side effect
```

Commands use explicit arguments, timeouts, captured output, retries where configured, redaction, and claim heartbeats. A retry does not erase the previous attempt; diagnostic metadata records what the operator needs to distinguish an application failure from a platform failure.

## Failure and Recovery Matrix

| Failure | Detection | Recovery behavior | Persisted evidence |
| --- | --- | --- | --- |
| Worker exits during a deployment | Claim stops refreshing and becomes stale. | Reconciler marks the attempt failed; the operator can inspect and retry or clean up. | Deployment error and reconciliation event. |
| Worker loses its claim during a long command | Ownership check fails at the next heartbeat or guarded write. | The old worker stops processing without overwriting the new owner. | Claim and lifecycle events already committed. |
| Clone, build, or test fails | External command returns non-zero or times out. | Build and deployment transition to failed. | Command metadata, output tail, duration, and failure event. |
| Registry push succeeds but verification fails | Remote image inspection fails separately from push. | Deployment fails before runtime apply. | Push success and verification failure remain distinct events. |
| Kubernetes reference is missing | Preflight cannot resolve a ConfigMap, Secret, or key. | Deployment fails before Helm changes the workload. | Preflight summary and structured metadata. |
| Helm rollout or readiness times out | Helm returns a failed upgrade/install result. | Deployment fails and Kubernetes diagnostics are collected. | Helm output, resource summaries, Pod state, events, and available logs. |
| A running Pod is deleted | Live health may fail and Kubernetes reports a missing/unready endpoint. | Deployment/ReplicaSet creates a replacement; persisted lifecycle remains running. | Live health is transient; refreshed diagnostics show the replacement Pod. |
| Stop is requested during deployment | Deployment worker observes the active stop command at a step boundary or heartbeat. | Deployment processing yields; command worker performs runtime cleanup and records stopped. | Deployment and command event timelines. |
| Runtime resources remain after an interrupted operation | Reconciler compares terminal database records with runtime state. | Eligible Docker/Kubernetes/Helm resources are removed. | Reconciliation cleanup event or failure detail. |
| Database is unavailable | Health endpoints and database operations fail. | Components remain unhealthy until connectivity returns; no external work should be inferred as committed. | Structured component logs; database evidence resumes after recovery. |

## Health Semantics

AutoDeploy intentionally separates three signals:

- platform readiness indicates whether the API can accept deployment work with its selected executor configuration;
- rollout health is checked during deployment and becomes part of the immutable deployment evidence;
- live workload health is a transient post-deploy probe and does not rewrite a running deployment as failed after a short outage.

For Kubernetes deployments, rollout health uses a temporary Service port-forward. This tests the workload and Service without making Ingress DNS or controller routing part of deployment success. The public Ingress URL is retained for operator access and live health after deployment.

## Reconciliation Boundaries

The reconciler repairs known, bounded inconsistencies; it is not a general desired-state engine. It can expire stale claims and remove supported leftover runtime resources. It does not reconstruct an external command result that was never committed, automatically retry arbitrary failed deployments, or guarantee cleanup of resources created outside the platform's naming and metadata contract.

Helm mode has an additional ownership rule because multiple deployment records refer to one stable release. Automatic cleanup uses a fresh database view and is skipped when a newer deployment exists. Explicit operator cleanup remains an authoritative action, so operators should select the current deployment that owns the workload.

## Operator Recovery Checklist

1. Confirm API, database, worker, and reconciler health.
2. Inspect the deployment summary and ordered events before retrying.
3. Review the build/runtime logs and Kubernetes diagnostics bundle.
4. Confirm whether runtime resources or a Helm release still exist.
5. Restore controlled failure variables or missing referenced resources.
6. Create a new deployment for recovery so the failed attempt remains visible.
7. Use the platform cleanup action for obsolete managed resources.

Operational commands and mode-specific troubleshooting are in the [Runbook](runbook.md). State transitions are defined in the [Deployment model](deployment.md), and trust boundaries are defined in [Security](security.md).
