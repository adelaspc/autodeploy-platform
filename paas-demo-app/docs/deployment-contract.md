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
