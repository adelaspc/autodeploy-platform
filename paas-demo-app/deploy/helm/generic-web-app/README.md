# Generic Web App Chart

This chart deploys one stateless user web workload managed by the PaaS.

It is intentionally stack-agnostic. It does not assume Python, Node, PHP, migrations, shell tools, or any framework-specific runtime behavior. By default, it lets the container image define its own `ENTRYPOINT` and `CMD`.

## Scope

This chart renders:

- one Deployment
- one Service
- optional Ingress

It does not render jobs, migrations, PVCs, RBAC, service accounts, Docker socket mounts, kubeconfig mounts, sidecars, autoscaling, cronjobs, or network policies.

## Relationship To The PaaS

`deploy/helm/paas-control-plane` deploys internal platform components such as the API, worker, reconciler, migration hook, workspace volume, RBAC, optional Docker socket access, and optional kubeconfig.

`deploy/helm/generic-web-app` is the desired workload abstraction for user applications managed by the PaaS.

The PaaS has a values-generation layer that maps the existing project/deployment/build model into this chart's values contract.

Current behavior:

- the Kubernetes executor defaults to direct generated Deployment and Service manifests
- when `CONTROL_PLANE_K8S_DEPLOYMENT_MODE=helm`, the executor renders generated values and deploys this chart with Helm
- per-variable ConfigMap and Secret key references are rendered through `env[].valueFrom`
- `envFrom.configMaps` and `envFrom.secrets` are not generated yet because the current project model does not have whole-resource import fields
- diagnostics and reconciliation still use the existing direct Kubernetes resource behavior

Helm mode follows this path:

```text
project spec -> generated values.yaml -> helm upgrade/install generic-web-app
```

Helm-managed user workloads use one stable release per project/environment workload, not one release per deployment attempt. The release name shape is:

```text
paas-<project-slug>-<environment-slug>-<project-id-suffix>
```

Redeploys upgrade the same release. Stop behavior uninstalls the release. Diagnostics and reconciliation are Helm-aware when persisted release metadata is available: running releases are checked with `helm status`, and failed or stopped deployments with leftover releases are cleaned up with `helm uninstall`.

## Validation

```bash
helm lint ./deploy/helm/generic-web-app
helm template generic ./deploy/helm/generic-web-app > /dev/null
helm template generic-minimal ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/minimal.yaml > /dev/null
helm template generic-node ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/node-express.yaml > /dev/null
helm template generic-python ./deploy/helm/generic-web-app -f ./deploy/helm/generic-web-app/examples/python-fastapi.yaml > /dev/null
```

Generated values from the PaaS mapper can be checked the same way:

```bash
helm template generic ./deploy/helm/generic-web-app -f <generated-values.yaml>
```

This render-only validation requires the Helm CLI but does not require a live Kubernetes cluster.

## Examples

Example values are available in:

- `examples/minimal.yaml`
- `examples/node-express.yaml`
- `examples/python-fastapi.yaml`
