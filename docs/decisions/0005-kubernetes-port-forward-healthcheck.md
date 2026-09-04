# ADR 0005: Kubernetes Healthcheck Through Service Port-Forward

**Status:** Accepted

## Context

A public Ingress depends on local DNS, controller configuration, host routing, and—in WSL2—the address visible from Windows. Making those layers part of rollout success would cause a healthy Pod and Service to fail deployment because of an unrelated access-path problem.

## Decision

After rollout, the Kubernetes executor opens a temporary port-forward to the generated Service and probes the project's healthcheck path through that connection. The public Ingress URL is still persisted for operator access and later live-health checks.

## Consequences

- Deployment health verifies the workload and Service independently from public routing.
- Ingress problems can be diagnosed separately from application readiness.
- Port allocation and port-forward process cleanup require explicit handling.
- A successful rollout healthcheck does not guarantee that external DNS or Ingress is reachable.
- Live health through the public URL may differ from the health result persisted during deployment.
