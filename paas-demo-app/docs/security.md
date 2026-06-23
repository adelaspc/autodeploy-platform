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

If none of those variables are configured, bearer-token auth is disabled. That mode is intended only for local development and test workflows.

## Route Boundary

Public:

- `GET /health`

Protected by bearer token:

- `/api/projects/**`
- `/health/db`
- `/health/platform`
- `/health/activity`

Webhook authentication is separate:

- `POST /api/webhooks/github` uses GitHub HMAC signature verification
- webhook requests do not use bearer API tokens

## Role Intent

- `read_only`: operator visibility endpoints, summaries, diagnostics, logs, and protected health/status routes
- `deployer`: deploy/retry/redeploy actions in addition to read access
- `admin`: project management and sensitive mutation actions in addition to deployer access

## Secret Handling Posture

- API tokens are read from environment configuration.
- Git clone credentials for private repositories are also environment-backed.
- The control plane compares API tokens using constant-time comparison.
- Raw bearer tokens should not be logged.
- Executor command logs redact configured sensitive values where supported by the worker path.

Project configuration now distinguishes readable config from secret-bearing config in `env_vars`:

- normal config can use literal values such as `{"name":"LOG_LEVEL","value":"info"}`
- local-style secret injection can use literal values plus `is_secret: true`
- Kubernetes secret delivery should use `value_source: "secret_key_ref"` with an existing Secret reference

Current redaction behavior:

- project list/show/create/update responses mask secret literal values as `[REDACTED]`
- deployment events, summaries, diagnostics, and log API responses redact known secret values
- audit metadata redacts sensitive keys and secret-bearing env var payloads
- error responses that include deployment metadata use the same redaction layer

Executor-specific behavior:

- `local-docker` still receives the real secret value for `docker run`, but the worker redacts that value from command metadata, deploy logs, runtime-log capture, and healthcheck summaries
- `kubernetes` does not allow literal secret values in rendered manifests; secret env vars must use `secret_key_ref`

Current storage tradeoff:

- when `is_secret: true` is used, the control plane stores the literal value in the project record so the local executor can inject it at runtime
- those values are masked on read rather than encrypted at rest
- this is an intentional portfolio-stage tradeoff because the project does not yet include key management, envelope encryption, or an external secret manager

Log-redaction boundaries:

- API responses, deployment read models, audit metadata, and worker command metadata are redacted before returning or persisting operator-visible data
- redaction is best-effort for values the control plane knows about through token config, project secret fields, registry credentials, and secret env vars
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
- deployment patch and stop actions
- denied mutating requests on protected project API routes

Audit records currently store:

- timestamp
- action
- actor role
- resource type and resource id
- success or failure status
- request id when passed through `X-Request-Id`
- client IP when available from the request context
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

## Current Tradeoffs

- No user accounts, sessions, OAuth, SSO, or JWT signing flow
- No per-token audit identity beyond the configured role
- No database-backed token rotation workflow
- No fine-grained per-project authorization boundaries
- No external secret manager integration yet
- Secret literal values marked with `is_secret: true` are masked on read but not encrypted at rest

These are intentional scope limits for this project stage. The current model is meant to demonstrate a credible operator-facing security boundary with clean code structure, not a full enterprise identity system.

## Suggested Next Hardening Steps

- extend the audit trail with more lifecycle and webhook-admission signals if needed
- move secrets to a dedicated secret manager or Kubernetes secret delivery pattern when presenting a more production-oriented setup
