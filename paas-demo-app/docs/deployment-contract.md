# Platform Contract

A deployable user application must:

- be hosted in a GitHub repository
- have a valid Dockerfile
- build into a container image
- push successfully to the configured container registry
- expose exactly one HTTP port
- provide a healthcheck endpoint
- be stateless
- use environment variables for runtime configuration
- store persistent data externally

## Development-Only Exception

For local development and integration testing, the control plane may accept a local filesystem repository path as `repo_url` when `CONTROL_PLANE_ENV=development`.

Outside development, project create and update requests must use supported remote Git repository URLs. Local filesystem paths are rejected in production-like environments.

At this stage, supported remote Git repositories are limited to canonical GitHub HTTPS URLs:

- `https://github.com/<owner>/<repo>`
- `https://github.com/<owner>/<repo>.git`

The control plane normalizes accepted GitHub repository URLs to `https://github.com/<owner>/<repo>.git` for storage and matching.

Private repositories are supported via GitHub HTTPS plus token auth. SSH Git authentication is not supported yet.

## Platform-Owned Workers

The platform is allowed and expected to run its own internal workers.

Examples:

- build worker
- deployment worker
- queue worker for deployment jobs
- healthcheck worker

These are platform infrastructure components, not user application workloads.

## User Application Workloads

In v1, a user application may only define one web workload.

User-managed background workers, queue consumers, scheduled jobs, and multi-process workloads are not supported in v1.
