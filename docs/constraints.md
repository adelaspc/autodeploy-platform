# Initial Constraints (v1)

- Dockerfile-based user applications only
- single environment
- no preview environments
- no autoscaling
- no persistent volumes
- no user-managed background worker workloads
- no user-managed queue consumers
- no user-managed scheduled jobs
- no multiple exposed ports
- no custom domains
- no SSL automation
- no buildpacks

## Non-Goals

- multi-environment support
- preview deployments
- buildpacks
- autoscaling
- persistent storage
- user-defined workers / jobs
- cron or scheduled tasks
- domains / SSL
- multiple services per application
