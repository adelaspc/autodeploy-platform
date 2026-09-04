# Data Model

The relational model separates reusable project configuration, build artifacts, runtime attempts, operator commands, and evidence. This lets the control plane preserve what happened during an old deployment even after the project is edited or its runtime resources are removed.

## Entity Relationship Diagram

```mermaid
erDiagram
    PROJECT ||--o{ BUILD : creates
    PROJECT ||--o{ PLATFORM_DEPLOYMENT : configures
    BUILD ||--o{ PLATFORM_DEPLOYMENT : supplies
    PLATFORM_DEPLOYMENT ||--o{ DEPLOYMENT_EVENT : records
    PLATFORM_DEPLOYMENT ||--o{ DEPLOYMENT_COMMAND : receives
    PLATFORM_DEPLOYMENT o|--o{ WEBHOOK_DELIVERY : may_result_from

    PROJECT {
        int id PK
        string name UK
        string repo_url
        string branch
        string git_auth_type
        string git_secret_ref
        json env_vars
        string trigger
        string runtime
    }

    BUILD {
        int id PK
        int project_id FK
        string commit_sha
        string status
        string image_ref
        string registry_push_status
        string build_log_path
    }

    PLATFORM_DEPLOYMENT {
        int id PK
        int project_id FK
        int build_id FK
        string environment
        string status
        json spec_snapshot_json
        string claimed_by
        datetime claimed_at
        string helm_release_name
        string service_url
    }

    DEPLOYMENT_EVENT {
        int id PK
        int deployment_id FK
        string event_type
        string step
        string status
        string level
        json metadata_json
    }

    DEPLOYMENT_COMMAND {
        int id PK
        int deployment_id FK
        string command_type
        string status
        string active_key
        string claimed_by
        datetime claimed_at
    }

    WEBHOOK_DELIVERY {
        int id PK
        string delivery_id UK
        int deployment_id FK
        string event_type
        string status
        string commit_sha
    }
```

`AUDIT_EVENT` is intentionally outside the foreign-key graph. It records security and operator actions using `resource_type` and `resource_id` strings, so audit evidence is not coupled to the lifecycle of one domain table.

## Entity Responsibilities

| Entity | Responsibility | Important invariant |
| --- | --- | --- |
| `Project` | Stores the mutable specification used to request future work. | Editing a project must not rewrite an existing deployment. |
| `Build` | Tracks source identity, build/test progress, registry publication, and artifact logs. | Build state is distinct from runtime deployment state. |
| `PlatformDeployment` | Tracks one deployment attempt, its immutable specification snapshot, worker claim, and runtime identity. | State changes must follow the deployment transition graph. |
| `DeploymentEvent` | Stores the ordered diagnostic timeline for a deployment. | Events remain available after failure or runtime cleanup. |
| `DeploymentCommand` | Represents asynchronous stop and cleanup work. | Only one active command of the same type may exist for a deployment. |
| `WebhookDelivery` | Records GitHub delivery IDs and their processing result. | A delivery ID is unique, making webhook retries idempotent. |
| `AuditEvent` | Records authenticated or security-relevant actions and request correlation. | Audit data is separate from the deployment event stream. |

## Mutable Specification and Immutable Attempts

`Project` is the current desired template. When the API accepts a deployment request, it resolves the source commit and stores a versioned copy of the relevant project fields in `PlatformDeployment.spec_snapshot_json`. The worker executes from that snapshot rather than rereading mutable project settings.

This boundary provides two useful properties:

- a queued deployment cannot silently change when the project is edited;
- old deployment records remain explainable because their repository, build, environment, resource, and authentication references are retained.

The snapshot contains references to secrets, not the referenced Kubernetes Secret values or Git token itself. API serialization and diagnostic surfaces apply secret-aware redaction.

## Build and Deployment Separation

A build answers: “Which source was tested and which image was produced?” A deployment answers: “What happened when that artifact was applied to a runtime?” Keeping both records makes build, registry, and runtime stages independently inspectable. The current failure policy may still mark the associated build failed when the overall execution cannot produce a usable deployment, while the earlier successful step events remain in the timeline.

The schema permits a build to be associated with more than one deployment record. This supports workflows that reuse an artifact without collapsing separate runtime attempts into one history entry.

## Evidence and Commands

`DeploymentEvent` is the deployment-specific timeline. It contains step names, statuses, human-readable summaries, and structured metadata used by the UI diagnostics views.

`DeploymentCommand` keeps stop and cleanup outside API request handlers. Its nullable `active_key` participates in a unique constraint with deployment and command type. While the value is `active`, a repeated request reuses the existing command; completion clears the key so a later command can be recorded without deleting history.

`AuditEvent` answers a different question from `DeploymentEvent`: who requested or accessed an operation, under which role and request ID. The two histories are complementary rather than duplicates.

## Deletion and Retention

Removing runtime resources does not delete the project, build, deployment, event, command, or audit records. Project deletion is a separate administrative action; its owned build, deployment, event, and command relationships are modeled with ORM cascades. Operational retention policies are documented in the [Runbook](runbook.md); runtime cleanup semantics are documented in the [Deployment model](deployment.md).

## Related Decisions

- [Database-backed asynchronous work](decisions/0001-database-backed-asynchronous-work.md)
- [Immutable deployment specification snapshots](decisions/0002-immutable-deployment-snapshots.md)
- [Stable Helm release identity](decisions/0004-stable-helm-release-identity.md)
