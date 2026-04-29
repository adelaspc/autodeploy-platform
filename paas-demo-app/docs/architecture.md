# Architecture

## Components

### Control Plane

Responsible for:

- storing application configuration
- creating deployment records
- tracking deployment status
- persisting deployment events
- orchestrating platform workers
- applying or coordinating Kubernetes changes

### Platform Worker

Responsible for:

- cloning repositories
- building Docker images
- running optional tests
- pushing images to the registry
- reporting step-level success or failure back to the control plane

The platform worker is an internal infrastructure component. It is not the same thing as a user-managed background worker workload.

### Container Registry

Responsible for:

- storing built application images
- providing immutable image references for Kubernetes deployments

### Kubernetes Cluster

Responsible for:

- running deployed user applications
- performing rollouts
- restarting failed pods
- exposing services internally or externally

### GitHub

Responsible for:

- hosting application source code
- providing repository, branch, and commit information
- optionally triggering deployments through webhooks in a future version
