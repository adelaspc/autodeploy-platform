# Architectural Decision Records

These records capture decisions that materially shape AutoDeploy. They document why the current design exists, the tradeoffs it accepts, and the conditions under which a future implementation might revisit it.

| ADR | Decision |
| --- | --- |
| [0001](0001-database-backed-asynchronous-work.md) | Persist asynchronous work and claims in MySQL |
| [0002](0002-immutable-deployment-snapshots.md) | Execute deployments from immutable specification snapshots |
| [0003](0003-runtime-executor-contract.md) | Isolate runtime modes behind one executor contract |
| [0004](0004-stable-helm-release-identity.md) | Use one stable Helm release per project and environment |
| [0005](0005-kubernetes-port-forward-healthcheck.md) | Verify Kubernetes rollout health through a temporary Service port-forward |

Each decision is currently **Accepted**. A future change should add a new ADR and mark the replaced record **Superseded** rather than rewriting the original rationale.
