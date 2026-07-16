# AutoDeploy Control Plane

[![CI](https://github.com/adelaspc/autodeploy-platform-/actions/workflows/ci.yml/badge.svg)](https://github.com/adelaspc/autodeploy-platform-/actions/workflows/ci.yml)

AutoDeploy is an operator-facing PaaS control plane that turns project specifications into observable, asynchronous deployments across simulated, local Docker, and Kubernetes runtimes.

![AutoDeploy operator console](docs/assets/operator-console.png)

## What this project demonstrates

- A Flask API that persists desired deployment state instead of running infrastructure commands in HTTP requests.
- A leased background worker with claim heartbeats, ownership checks, retries, and explicit failure reasons.
- Reconciliation of stale claims, missing workloads, and leftover Docker or Kubernetes resources.
- Immutable deployment snapshots, lifecycle validation, and a detailed event stream for every build and rollout step.
- Docker build/test/run and Kubernetes manifest or Helm deployment paths behind one executor contract.
- A Vue operator console for project configuration, deployment actions, logs, health, events, and diagnostics.
- Bearer-token roles, audit records, request correlation, secret redaction, namespace-scoped RBAC, and opt-in Docker socket access.
- CI coverage for Python and frontend tests, lint, migrations, dependency/security checks, container builds, Compose, and Helm templates.

## Architecture at a glance

```mermaid
flowchart LR
    O[Operator / Vue console] --> A[Flask API]
    A <--> D[(Control-plane DB)]
    W[Worker] <--> D
    R[Reconciler] <--> D
    W --> G[Git repository]
    W --> X{Executor}
    X --> F[Fake runtime]
    X --> L[Local Docker]
    X --> K[Kubernetes / Helm]
    A -. status, events, logs .-> O
```

The API records intent, the worker owns side effects, and the reconciler repairs drift. Deploy, retry, redeploy, stop, and cleanup all cross the same persisted ownership boundary. See [Architecture](docs/architecture.md) for claims, snapshots, execution, reconciliation, and runtime boundaries.

## Five-minute quick start

This path uses SQLite and the `fake` executor. It needs Python 3.12 and Node.js 22, but no Docker daemon, registry, MySQL, or Kubernetes cluster.

```bash
git clone https://github.com/adelaspc/autodeploy-platform-.git
cd autodeploy-platform-

python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt

make env-init
make db-upgrade PROFILE=demo

cd frontend
npm ci
cd ..
```

Start the three processes in separate terminals:

```bash
make api PROFILE=demo
```

```bash
make worker PROFILE=demo
```

```bash
cd frontend && npm run dev
```

Open `http://127.0.0.1:5173`. Create a project with these minimum values:

| Field | Value |
| --- | --- |
| Name | `control-plane-demo` |
| Repository | the absolute path to this clone |
| Branch | your current local branch, usually `main` |
| Port | `5000` |
| Healthcheck | `/health` |

Save the project, select **Deploy**, and watch the fake worker move it to `running`. The console shows the deployment summary and persisted events without building or publishing an image. Local paths are accepted only in the development profile; production-style configurations accept canonical GitHub HTTPS URLs.

To exercise the API directly instead, create and deploy a project with:

```bash
ROOT="$(pwd)"
curl -fsS http://127.0.0.1:5000/api/projects \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"control-plane-demo\",\"repo_url\":\"$ROOT\",\"branch\":\"$(git branch --show-current)\",\"port\":5000,\"healthcheck_path\":\"/health\"}"

curl -fsS -X POST http://127.0.0.1:5000/api/projects/1/deploy \
  -H 'Content-Type: application/json' -d '{}'

curl -fsS http://127.0.0.1:5000/api/projects/1/deployments/latest
curl -fsS http://127.0.0.1:5000/api/projects/1/deployments/1/events
```

The demo profile explicitly disables bearer authentication for local development. Never carry that setting into a production-like environment.

## Execution modes

| Mode | What it proves | Infrastructure |
| --- | --- | --- |
| `fake` | API, persistence, worker claims, lifecycle, events, UI | SQLite only |
| `local-docker` | Clone, image build/test, optional push, container health and logs | Docker; registry optional |
| `kubernetes` | Image publish, manifests or Helm, rollout, diagnostics, ingress and cleanup | Docker, registry, Kubernetes |

Profiles are selected with `PROFILE=demo`, `PROFILE=local-docker`, or `PROFILE=local-kubernetes`. The complete configuration and validated operating procedures live in the [Runbook](docs/runbook.md).

## Common verification commands

```bash
# Fast backend tests and lint
make test
make lint

# Frontend unit/component tests and production build
make frontend-test
make frontend-build

# Container build
docker build --tag autodeploy-control-plane:local .
```

Infrastructure-dependent suites are opt-in: `make integration-docker`, `make integration-mysql`, and `make integration-kubernetes`. See [Development](docs/development.md) for dependency locks, repository layout, all local checks, and the CI job map.

## Security boundaries and non-goals

This is a credible single-operator/platform portfolio system, not a hardened public multi-tenant PaaS. API tokens implement `read_only`, `deployer`, and `admin` roles; Git/registry secrets come from ignored files or Kubernetes Secrets; response and log surfaces redact known secret values; and executable project commands reject shell control syntax.

Docker socket access and kubeconfig access are deliberate trusted-operator boundaries. Socket mounting is disabled in the base Compose and Helm configurations and should never be enabled for untrusted repositories. Namespace-per-tenant isolation, OAuth/user management, encrypted database secrets, sandboxed remote builds, TLS automation, GitOps, autoscaling, and multi-cluster scheduling are outside the current scope. See [Security](docs/security.md) for the complete threat and trust boundary discussion.

## Documentation

| Document | Canonical topic |
| --- | --- |
| [Architecture](docs/architecture.md) | System context, deployment execution, claims, reconciliation, runtime boundaries |
| [API reference](docs/api-reference.md) | Endpoints, roles, filters, pagination, response conventions, webhooks |
| [Deployment model](docs/deployment.md) | Lifecycle states, valid transitions, failure reasons, deployment events |
| [Runbook](docs/runbook.md) | Configuration, database, health, observability, Compose, Kubernetes, troubleshooting |
| [Security](docs/security.md) | Authentication, roles, audit, correlation, secrets, Docker socket, Kubernetes RBAC |
| [Development](docs/development.md) | Layout, setup, dependency locks, tests, builds, CI jobs |
| [Portfolio demo](docs/demo-script.md) | Guided reviewer demonstration |
| [Manual MicroK8s validation](docs/microk8s-manual-validation.md) | End-to-end local Kubernetes validation |
| [Platform contract](docs/deployment-contract.md) | Control-plane, worker, and user-workload ownership boundaries |
| [Application specification](docs/specs.md) | Supported project fields and workload assumptions |
| [Constraints](docs/constraints.md) | Product and implementation constraints |
| [Future work](docs/future.md) | Deliberately deferred capabilities |

The API and deployment model are intentionally stable enough to inspect, while the fake path keeps first contact independent of platform infrastructure.
