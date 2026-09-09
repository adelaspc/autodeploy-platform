# Security Notes

## Goal

This project uses a deliberately small control-plane security model intended for a portfolio-grade backend and DevOps artifact, not a commercial identity platform.

## API Authentication Model

- API authentication uses `Authorization: Bearer <token>`.
- Tokens are configured through environment variables rather than stored in the database.
- Supported roles are:
  - `read_only`
  - `deployer`
  - `admin`
- Authorization is role-based and route-scoped.

Current token environment variables:

- `CONTROL_PLANE_API_TOKEN_READ_ONLY`
- `CONTROL_PLANE_API_TOKEN_DEPLOYER`
- `CONTROL_PLANE_API_TOKEN_ADMIN`
- `CONTROL_PLANE_API_TOKENS_JSON`

The role-specific variables are the simplest path when one token per role is enough. `CONTROL_PLANE_API_TOKENS_JSON` supports multiple named tokens:

```json
[
  {"name": "ops-read", "role": "read_only", "token": "replace-me"},
  {"name": "ci-deployer", "role": "deployer", "token": "replace-me"},
  {"name": "break-glass-admin", "role": "admin", "token": "replace-me"}
]
```

The JSON token list is additive with the role-specific variables. Token names are optional operator metadata and are not returned by health/readiness responses.

If none of those variables are configured, the app fails fast unless `CONTROL_PLANE_ALLOW_AUTH_DISABLED=true` is explicitly set in a local environment (`CONTROL_PLANE_ENV=development`, `local`, or `test`). Auth-disabled mode is intended only for local development and test workflows.

## Route Boundary

Public:

- `GET /health`
- `GET /health/ready`

Protected by bearer token:

- `/api/projects/**`
- `/health/db`
- `/health/platform`
- `/health/activity`
- `/health/observability`
- `/api/audit-events`

Webhook authentication is separate:

- `POST /api/webhooks/github` uses GitHub HMAC signature verification
- webhook requests do not use bearer API tokens

Health response boundaries:

- `/health/db` returns only database reachability and a stable error code; raw database driver errors are logged internally and are not returned to clients

## Role Intent

- `read_only`: operator visibility endpoints, summaries, diagnostics, logs, and protected health/status routes
- `deployer`: deploy/retry/redeploy actions in addition to read access
- `admin`: project management and sensitive mutation actions in addition to deployer access

## Secret Handling Posture

- API tokens are read from environment configuration.
- API tokens can be configured either as one token per role or as a JSON list of named role tokens.
- Git clone credentials for private repositories are also environment-backed.
- The control plane compares API tokens using constant-time comparison.
- Raw bearer tokens should not be logged.
- The operator UI keeps its bearer token in `sessionStorage`, clears any legacy `localStorage` value, and therefore does not retain the token after the browser session ends. Browser storage remains readable by same-origin JavaScript, so the Content Security Policy and avoidance of unsafe HTML sinks remain part of this boundary.
- Executor command logs redact configured sensitive values where supported by the worker path.

Project configuration now distinguishes readable config from secret-bearing config in `env_vars`:

- normal config can use literal values such as `{"name":"LOG_LEVEL","value":"info"}`
- local-style secret injection can use literal values plus `is_secret: true`
- Kubernetes secret delivery should use `value_source: "secret_key_ref"` with an existing Secret reference

Current redaction behavior:

- project list/show/create/update responses mask secret literal values as `[REDACTED]`
- deployment events, preflight summaries and metadata, deployment summaries, diagnostics, and log API responses redact the project secret values supplied to their serialization path
- audit metadata redacts sensitive keys and secret-bearing env var payloads
- error responses that include deployment metadata use the same redaction layer

Executor-specific behavior:

- `local-docker` still receives the real secret value for `docker run`, but the worker redacts that value from command metadata, deploy logs, runtime-log capture, and healthcheck summaries
- `kubernetes` does not allow literal secret values in rendered manifests; secret env vars must use `secret_key_ref`

Current storage tradeoff — **Accepted risk for local/portfolio use; not production-approved**:

- when `is_secret: true` is used with a literal value, the control plane stores that value in plaintext in `Project.env_vars` so the local executor can inject it at runtime
- each new deployment copies the project environment into `PlatformDeployment.spec_snapshot_json`; consequently, a literal secret can exist in both the mutable project row and one or more historical deployment snapshots
- database replicas, exports, and backups can retain the same plaintext values after a project or deployment is removed from the live database
- API masking and log redaction reduce accidental disclosure but do not encrypt stored values and do not protect against an actor with direct database or backup access
- global API, webhook, Git, metrics, and registry credentials remain environment/Kubernetes-Secret backed and are not intentionally persisted by this mechanism
- `secret_key_ref` and `configmap_key_ref` store resource names and keys only; the referenced Kubernetes value is not copied into the control-plane database
- this is an explicit portfolio-stage risk acceptance because the project does not yet include key management, envelope encryption, or an external secret manager; do not use real production credentials as literal project values

Compensating controls for the accepted scope are restricted database and backup access, secret-aware API/log/audit redaction, the Kubernetes-mode prohibition on literal secret values, and operator guidance to prefer `secret_key_ref`. This decision must be revisited before multi-tenant use, untrusted operator access, regulated data, or production credentials. An acceptable production design would either reject literal secrets entirely or encrypt each value with a data-encryption key protected by a KMS/secret-manager key, including key versioning and rotation.

Log-redaction boundaries:

- API responses, deployment read models, audit metadata, and worker command metadata apply redaction before returning or persisting operator-visible data
- general deployment/event/read-model paths receive literal project environment values marked `is_secret`; they do not automatically receive the registry password, API/metrics tokens, webhook secret, or every configured Git token
- mapping redaction masks values under credential-like keys such as `password`, `token`, and `authorization`; log redaction additionally masks common bearer, key/value credential, and URL-userinfo shapes, even when the exact value was not supplied
- Helm failure diagnostics are a deliberate wider boundary: they explicitly collect project secret values, configured global Git/API/metrics/webhook/registry values available to that process, and the executor registry password before sanitizing captured diagnostic output
- redaction remains best-effort and path-dependent; configuration alone does not make a global credential known to every redaction call
- the platform cannot redact an unknown secret if a user application prints it under an unrelated value or generates it independently at runtime
- for Kubernetes deployments, prefer `secret_key_ref` entries so secret values stay in Kubernetes Secrets instead of the control-plane database

Token rotation guidance:

- configure API tokens through environment variables or Kubernetes Secrets, never in committed values files
- rotate by updating the relevant secret value, restarting the API pods, and then updating clients to use the new token
- during a manual rotation window, temporarily configure the replacement token in a higher or equivalent role only when operationally necessary, then remove the old value promptly
- treat webhook secrets and Git clone tokens as separate credentials with separate rotation steps

Future hardening options:

- move secret delivery to Kubernetes Secrets for all cluster deployments
- use an external secret manager such as Vault or a cloud KMS-backed secret service
- add secret rotation workflows and operational runbooks
- add encryption-at-rest only alongside real key-management boundaries

## Auditability

The control plane records a small audit trail for important mutating API actions.

Current audit coverage includes:

- project create, update, and delete
- deploy, retry, and redeploy actions
- manual deployment record creation
- generic deployment patch actions
- dedicated deployment stop and Kubernetes cleanup actions
- denied mutating requests on protected project API routes

The audit trail records who requested an API action and the associated request context. Worker-owned asynchronous progress and completion are recorded as deployment events rather than duplicated as audit records; for example, a stop request is audited as `deployment.stop_requested`, while the worker emits `deployment.stop_started` and `deployment.stopped`.

Audit records currently store:

- timestamp
- action
- actor role
- resource type and resource id
- success or failure status
- request id when passed through `X-Request-Id`
- client IP when available from the request context

The audit trail uses the direct peer address by default and ignores client-supplied `X-Forwarded-For`. When the API is reachable only through trusted reverse proxies, `CONTROL_PLANE_TRUSTED_PROXY_COUNT` may be set to the exact number of proxy hops; Werkzeug then resolves `remote_addr` from the trusted right-hand side of the forwarding chain. Leaving the API directly reachable while enabling this setting would allow spoofing, so the default is `0` for local Compose and direct deployments.
- a small metadata object

What is intentionally not stored:

- raw bearer tokens
- secret values
- environment variable payloads
- registry passwords
- full webhook payload bodies

Audit metadata is sanitized with default redaction rules for sensitive keys such as `token`, `secret`, `password`, `authorization`, and `env_vars`.

## Request Correlation

The control plane uses lightweight request correlation with `X-Request-ID`.

Current behavior:

- accepts a valid incoming `X-Request-ID`
- replaces missing, malformed, or oversized request ids with a generated safe id
- returns `X-Request-ID` on every HTTP response
- records request ids in audit events
- includes request ids in request-completion logs

This feature is intended to connect:

- an incoming API request
- the HTTP response
- application logs
- audit records

It is intentionally not full distributed tracing. The control plane does not currently implement spans, trace propagation, OpenTelemetry, or external tracing backends.

## Metrics Authentication and Data Boundaries

`/metrics` is disabled by default and uses a dedicated bearer token when enabled. The metrics token is not shared with operator API roles and belongs in `.env.secrets` or a Kubernetes Secret.

Prometheus labels are limited to bounded operational dimensions. Request, project, deployment and user identifiers, application/repository/branch names, image tags, and error messages are excluded to avoid high cardinality and accidental data disclosure. Those values may still appear in redacted structured logs, deployment events, or audit records where record-oriented context is appropriate.

## Live Workload Health Boundary

The protected deployment `live-health` route probes only the worker-owned `service_url` recorded by the platform for an existing deployment and appends that project's validated healthcheck path. The HTTP request originates from the control-plane API process; the browser only requests this protected route. The generic deployment PATCH endpoint cannot modify this URL. The probe accepts only a plain HTTP(S) origin without credentials, query, fragment, or base path, and it does not follow redirects. Local executor URLs may intentionally resolve to loopback or private addresses, so those address ranges cannot be rejected without breaking the trusted local demo. Results contain only status, safe message, HTTP status, and check timestamp; response bodies are neither returned nor persisted.

API request bodies are capped at 2 MiB by default through `CONTROL_PLANE_MAX_CONTENT_LENGTH`. This limit applies before webhook payloads are read into memory. Browser-facing responses include a same-origin Content Security Policy, clickjacking protection, MIME-sniffing protection, and a no-referrer policy.

Live health is an operator signal, not an authorization or lifecycle transition. A single timeout or HTTP error does not mark a deployment failed. This avoids turning transient Kubernetes Pod replacement into destructive control-plane state.

## Command Execution Boundary

Project and deployment commands are treated as narrow local-demo build/test inputs, not arbitrary shell scripts. The API validates `default_test_command`, deployment `test_command`, and `migration_command` as direct argv-style commands with a 255-character limit.

Accepted examples include `pytest -q`, `python -m pytest`, `python -m py_compile server.py`, `npm test`, and `ruff check .`.

Rejected examples include shell wrappers and shell control syntax such as `sh -c`, `bash -c`, `&&`, `||`, `|`, redirection, backticks, and subshells. The worker applies the same validation before running tests so an invalid test command already present in the database fails at the `tests` step before `docker run` is invoked. `migration_command` remains a validated and stored compatibility field reserved by the application specification; the worker does not execute or revalidate it.

## Helm-Managed Runtime Secrets

The control-plane Helm chart supports two mutually exclusive runtime Secret modes:

- `secrets.create=true`: the chart creates the Secret from `secrets.values`; this mode is intended for local demos in a controlled, trusted environment.
- `secrets.create=false` with `secrets.existingSecret`: the chart references a Secret managed outside the Helm release and does not create, modify, or delete it.

The chart fails rendering when `secrets.create=true` is combined with `secrets.existingSecret`, or when `secrets.create=false` has no external Secret name. The components can reference one shared Secret object, but the chart does not import that entire object with `envFrom`. It creates individual `secretKeyRef` entries so each process receives only its intended keys:

| Component | Secret-backed environment |
| --- | --- |
| API | database URL, webhook secret, API role tokens/token JSON, metrics token, configured Git tokens |
| Worker | database URL, registry username/password, configured Git tokens |
| Reconciler | database URL |
| Migration Job | database URL |

Git credentials are intentionally available to both API and worker because API-side deploy and webhook admission resolve source revisions, while the worker clones the selected revision. With chart-managed secrets, non-empty keys matching `CONTROL_PLANE_GIT_TOKEN_*` are selected automatically. With `secrets.existingSecret`, their names must be listed in `secrets.gitTokenKeys` because Helm cannot inspect an external Secret during template rendering.

All four commands construct the shared Flask application. Startup validation therefore applies API-token and metrics-token requirements only when `CONTROL_PLANE_COMPONENT=api`; otherwise migration, worker, and reconciliation could not start after scoping. Protected routes still fail closed when tokens are absent, even if a non-API process were accidentally exposed as an HTTP server.

Docker Compose applies the same process-level split. `.env.secrets` is an input to Compose interpolation, not a whole-file `env_file` mounted into every service. The committed Compose file maps the documented `CONTROL_PLANE_GIT_TOKEN_GITHUB` key to API and worker; a custom `git_secret_ref` requires an explicit matching environment mapping for those two services.

The local `values.secrets.local.yaml` workflow has important boundaries:

- the file contains secret values in plaintext; Git ignore rules and mode `0600` reduce accidental exposure but do not encrypt it
- Kubernetes Secrets are base64-encoded and are not strongly encrypted by default
- actual cluster-side protection depends on namespace/cluster RBAC and optional encryption at rest for etcd
- users and service accounts with sufficient access can read Kubernetes Secret values
- Helm stores release information in the cluster, including values supplied to the release, so sufficiently privileged users can recover chart-managed secret values
- this mode is suitable only for local, trusted evaluation; shared or production-like environments should use `secrets.existingSecret` with an external secret-management workflow

When `secrets.existingSecret` is used, Helm receives and stores the Secret name but not its data, provided secret values are not also passed through other Helm values or command-line arguments.

This limits credentials present in each process environment; it is not a complete Kubernetes authorization split. Worker and reconciler currently share the chart ServiceAccount, whose namespace Role can read referenced Secrets for workload validation. Separate ServiceAccounts and narrower Roles remain a stronger production hardening option.

## Docker Socket Boundary

The local Docker executor and the local MicroK8s demo can use `/var/run/docker.sock` so the worker can build, test, tag, push, and run workload images from inside the control-plane runtime. This is intentionally a trusted local-demo shortcut.

Docker socket access is a major trust boundary. A process that can talk to the host Docker daemon can start containers, mount host paths, inspect or remove containers, and potentially read host or registry material reachable by Docker. Do not use this mode with untrusted repositories, Dockerfiles, test commands, or workload images.

Current posture:

- Docker Compose does not mount the socket by default. Local profiles that need Docker-backed builds opt into `docker-compose.docker-socket.yml` with `CONTROL_PLANE_COMPOSE_DOCKER_SOCKET_ENABLED=true`.
- The base control-plane Helm chart does not mount the Docker socket by default.
- `values.local-microk8s.yaml` keeps the socket disabled because it uses the fake executor to validate the control-plane chart without building workloads.
- A production-oriented build path should replace this with an isolated builder such as a dedicated Kubernetes build Job, rootless BuildKit, Kaniko-style builder, or external CI build.

## Kubernetes RBAC Boundary

The control-plane Helm chart uses a namespace-scoped Role rather than cluster-wide permissions. The default Role is designed for manifest mode:

- ConfigMaps and Secrets are read-only so the worker can validate existing references before deploy.
- Pods and Pod logs are read-only for diagnostics.
- Pod port-forward creation is allowed for the temporary healthcheck tunnel.
- Services, Deployments, and optional Ingresses can be created, updated, patched, and deleted because they are the managed workload resources.

Helm mode is a separate trust boundary. Helm v3 stores release metadata in Secrets by default, so Secret mutation is not granted unless `rbac.helmReleaseStorage=true` is set explicitly. Cluster-scoped resources such as `IngressClass` are not granted by the namespace Role; local demos normally rely on the configured kubeconfig for that read.

## Current Tradeoffs

- No user accounts, sessions, OAuth, SSO, or JWT signing flow
- No per-token audit identity beyond the configured role
- No database-backed token rotation workflow
- No fine-grained per-project authorization boundaries
- No external secret manager integration yet
- Secret literal values marked with `is_secret: true` are masked on read but not encrypted at rest
- Auth-disabled mode exists only as an explicit local/test opt-in; production-like environments must configure bearer tokens
- Docker socket access remains a trusted local-demo boundary, not a production isolation model

These are intentional scope limits for this project stage. The current model is meant to demonstrate a credible operator-facing security boundary with clean code structure, not a full enterprise identity system.

The plaintext secret-literal limitation is specifically an **accepted local/portfolio risk**, not a resolved finding and not a production security guarantee.

## Suggested Next Hardening Steps

- extend the audit trail with more lifecycle and webhook-admission signals if needed
- move secrets to a dedicated secret manager or Kubernetes secret delivery pattern when presenting a more production-oriented setup
- replace Docker-socket-backed builds with an isolated builder before presenting the project as production-ready
