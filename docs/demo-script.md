# Portfolio Demo Script

This script is a repeatable 3–5 minute walkthrough of the local PaaS control plane. The platform is the product; the selected sample application only exposes platform behavior.

## Before Recording

Use the `local-kubernetes` profile and complete these checks before sharing the screen:

```bash
make wsl-ingress-domain
make compose-up PROFILE=local-kubernetes
make k8s-demo-check PROFILE=local-kubernetes
```

`make compose-up` stays attached. Run the readiness check in another terminal, or start Compose before recording.

Confirm:

- MicroK8s and Ingress are ready;
- the current WSL `nip.io` domain is configured;
- the Docker Hub workload repository is public or otherwise writable under the active plan;
- the selected demo workload uses its configured port and healthcheck path;
- the operator API token is saved in the UI if API authentication is enabled;
- no terminal, browser tab, or editor view exposes `.env.secrets`, tokens, passwords, or Secret values.

Use these healthy workload variables:

```text
APP_ENV=demo
APP_VERSION=1.0.0
APP_COMMIT_SHA=<current-workload-commit>
FEATURE_MESSAGE=Running through the local PaaS
DEMO_STARTUP_DELAY_SECONDS=0
DEMO_FAIL_STARTUP=false
DEMO_HEALTH_STATUS=healthy
DEMO_RESPONSE_DELAY_MS=0
```

If `DEMO_SECRET` is part of the recording, configure it through `secret_key_ref`. Do not use or show a literal secret.

## Main Recording — 3–5 Minutes

### 1. Frame the project — 20 seconds

Open the Overview and say:

> This is a local operator-facing PaaS control plane. The API persists desired work, the worker builds and deploys immutable images, and MicroK8s runs the workload. It is deliberately scoped as a portfolio system rather than a public multi-tenant platform.

Point out:

- API/platform readiness;
- active executor: `kubernetes`;
- API authentication posture;
- the Observability panel: structured logs, request correlation, metrics posture, and operational activity.

### 2. Show the workload contract — 25 seconds

Open the sample workload project and briefly show:

- private GitHub repository with token reference, if used;
- Dockerfile build context;
- port `5000`;
- healthcheck `/health`;
- environment variables and Secret reference support;
- CPU/memory fields and selected deployment mode.

Do not spend time editing every field. The point is that application configuration is declarative and separate from control-plane configuration.

### 3. Run a happy-path deployment — 60–90 seconds

Trigger **Run deploy / test**.

Narrate the persisted event sequence as it appears:

1. resolve and clone the exact Git commit;
2. build the Docker image with BuildKit;
3. run the configured test command;
4. tag and push the immutable commit-based image;
5. verify the remote image with `docker buildx imagetools inspect`;
6. validate referenced ConfigMaps and Secrets;
7. apply the Kubernetes workload;
8. wait for rollout;
9. healthcheck through a temporary Service port-forward;
10. persist Pod diagnostics and mark the deployment `running`.

Show Deployment History, Summary, Events, and the image digest/tag. Mention that command attempts, durations, return codes, output tails, and failure metadata are persisted and redacted.

### 4. Open the running workload — 30 seconds

Select **Open service** and show the workload response:

- version and commit;
- environment;
- hostname/Pod;
- uptime;
- public configuration;
- Secret mounted/not-configured state without its value;
- current failure posture.

Return to the PaaS and point out the selected deployment’s live health result. Clarify that initial rollout health is persisted, while this post-deploy signal is transient and polled separately.

### 5. Demonstrate Kubernetes self-healing — 45–60 seconds

Copy the exact Pod name from Kubernetes Diagnostics. Delete only that Pod:

```bash
kubectl delete pod <exact-pod-name-from-diagnostics> --namespace default
```

Do not delete by a broad label and do not delete the ReplicaSet.

In the PaaS, show:

- live health transition from `healthy` to `unhealthy`, if the outage lasts long enough for a poll;
- deployment lifecycle remains `running`;
- Kubernetes Deployment/ReplicaSet creates a replacement Pod;
- live health returns to `healthy`.

Refresh diagnostics and compare the new Pod name or open the workload again to show the changed hostname.

Say:

> The health signal is intentionally separate from lifecycle state. A transient Pod replacement should recover, not permanently rewrite deployment history as failed.

### 6. Close with diagnostics and cleanup — 30 seconds

Show:

- structured Pod fields: phase, container reason, restart count, images, and imagePullSecrets;
- build/runtime log tails;
- **Copy bundle** or **Download bundle**;
- **Cleanup Kubernetes resources** and explain that runtime resources are removed while build, event, audit, and diagnostic history is retained.

Close with:

> The project demonstrates the control-plane boundaries end to end: authenticated configuration, immutable build and registry flow, Kubernetes deployment, health and failure diagnostics, reconciliation, observability, and safe cleanup.

## Optional Controlled Failure Takes

Record these separately rather than forcing all of them into the main walkthrough.

### Configuration update

Change:

```text
APP_VERSION=1.1.0
FEATURE_MESSAGE=Configuration update deployed successfully
```

Save and redeploy. Show that the new workload receipt reflects the updated values and that deployment history retains the previous release.

### Slow startup

Set:

```text
DEMO_STARTUP_DELAY_SECONDS=8
```

Deploy with healthcheck path `/health`. Show the rollout waiting until the workload becomes ready. Restore `0` afterward.

### Healthcheck failure and recovery

Set:

```text
DEMO_HEALTH_STATUS=failed
```

Deploy and show the failed healthcheck, events, logs, and Kubernetes diagnostics. Restore `healthy` and deploy again to demonstrate recovery.

### CrashLoopBackOff and previous logs

Set:

```text
DEMO_FAIL_STARTUP=true
```

Deploy and show:

- rollout failure;
- container reason/restart count;
- current and previous Pod logs;
- likely-cause guidance in Diagnostics.

Restore `false`, redeploy, and show the healthy replacement.

## Recovery Checklist

Return the project to:

```text
DEMO_STARTUP_DELAY_SECONDS=0
DEMO_FAIL_STARTUP=false
DEMO_HEALTH_STATUS=healthy
DEMO_RESPONSE_DELAY_MS=0
```

Then:

```bash
make k8s-demo-check PROFILE=local-kubernetes
kubectl get pods,deployments,services,ingresses --namespace default
```

Use the PaaS cleanup action for obsolete demo deployments. Delete or scale a Deployment only when intentionally stopping a workload; deleting a managed Pod alone triggers Kubernetes self-healing.

## Recording Notes

- Keep one terminal for safe `kubectl` commands and one browser window for the PaaS/workload.
- Avoid showing long build output; use the event timeline and summaries.
- Keep one controlled failure in history because it makes diagnostics and recovery visible.
- Use exact Pod names for deletion.
- Never display local environment files, Docker Hub tokens, GitHub tokens, bearer tokens, kubeconfig contents, or Kubernetes Secret values.
