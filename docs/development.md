# Development

This page is the canonical source for repository setup, dependency management, local verification, and CI. Runtime deployment procedures belong in the [Runbook](runbook.md).

## Repository layout

```text
control_plane/   Flask API, application services, models, persistence
worker/          claims, deployment execution, commands, reconciliation
frontend/        Vue 3 and Vite operator console
migrations/      Alembic/Flask-Migrate history
tests/           fast tests plus opt-in infrastructure contracts
deploy/          control-plane and generic workload Helm charts
docs/            architecture, API, security, operations, and lifecycle
scripts/         local validation and runtime helpers
```

## Toolchain and installation

CI and the documented local path use Python 3.12 and Node.js 22.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
make env-init

cd frontend
npm ci
cd ..
```

`make env-init` copies committed examples into ignored `.env.demo`, `.env.local-docker`, `.env.local-kubernetes`, and `.env.secrets` files, generates missing local MySQL passwords, applies mode `0600`, and never overwrites an existing file.

## Dependency sources and locks

- `requirements.txt` declares direct Python runtime dependencies.
- `requirements.lock.txt` is the fully resolved, hashed runtime set installed in the image.
- `requirements-dev.txt` adds pinned test, lint, audit, and lock-generation tools for local development and CI.
- `frontend/package-lock.json` is the authoritative npm lock; use `npm ci` for reproducible installation.

After an intentional Python runtime dependency change, regenerate the lock with:

```bash
make lock-requirements
```

Review lock-file changes like source changes. Do not hand-edit resolved hashes.

## Local processes

For infrastructure-free development, initialize and migrate the demo profile, then run each long-lived process in its own terminal:

```bash
make db-upgrade PROFILE=demo
make api PROFILE=demo
make worker PROFILE=demo
make reconciler-loop PROFILE=demo
cd frontend && npm run dev
```

The frontend dev server proxies API requests to Flask. Built container images serve the compiled console through the backend. Profile configuration and Compose/Kubernetes operation are covered by the [Runbook](runbook.md).

## Verification commands

```bash
# Fast backend suite (excludes Docker, MySQL, and Kubernetes markers)
make test

# Backend coverage report
make coverage

# Python correctness lint
make lint

# Frontend unit/component tests and bundle
make frontend-test
make frontend-build

# Control-plane image
docker build --tag autodeploy-control-plane:local .
```

Run infrastructure contracts only when their prerequisite is intentionally available:

```bash
make integration-docker
CONTROL_PLANE_TEST_MYSQL_URL='mysql+pymysql://...' make integration-mysql
CONTROL_PLANE_TEST_KUBECONFIG=/path/to/kubeconfig make integration-kubernetes
```

The Kubernetes contract is read-only: it checks API readiness, server version, required resources, and effective permissions without creating cluster resources.

Useful deployment-asset checks:

```bash
make compose-config PROFILE=demo
helm template ci ./deploy/helm/autodeploy-control-plane \
  -f ./deploy/helm/autodeploy-control-plane/values.ci.yaml >/dev/null
helm lint ./deploy/helm/generic-web-app
helm template generic ./deploy/helm/generic-web-app >/dev/null
```

Security checks available locally include:

```bash
.venv/bin/bandit -c bandit.yaml -r control_plane worker wsgi.py
.venv/bin/pip-audit -r requirements.lock.txt
cd frontend && npm audit --omit=dev
make secret-scan
```

## CI job map

GitHub Actions runs on pull requests and pushes to `main`:

| Job | Contract |
| --- | --- |
| Gitleaks | No committed secrets in the working tree |
| Tests | Fast Python suite and informational XML coverage |
| Ruff | Focused Python correctness checks |
| Frontend | Locked install, tests, and production bundle on Node 22 |
| Bandit | Targeted static security scan of executable Python code |
| Migrations | Upgrade a fresh SQLite database through current head |
| MySQL concurrency | Two workers cannot claim the same pending row |
| Docker integration | Real build, deploy, health, stop, and failure-log flows |
| Dependency Audit | Informational Python and production npm advisory scans |
| Docker Build | Image build plus informational high/critical vulnerability scan |
| Compose Config | Rendered Compose configuration is valid |
| Helm Template | Control-plane and workload charts render/lint successfully |

Advisory databases change independently of this repository, so dependency and image vulnerability audits are informational. Runtime image/tool versions and action revisions remain pinned so upgrades arrive as explicit reviewable changes.

## Contribution hygiene

Keep generated runtime state in ignored profile, instance, workspace, log, and frontend build directories. Before handing off a documentation-only change, run at least `git diff --check` and a relative-link check; code changes should also run the checks proportional to the touched surface.
