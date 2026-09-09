# Portfolio Demo Script

This script supports both a repeatable 3–5 minute walkthrough and the shorter scenario recordings linked from the public demo page. The platform is the product; the selected sample application only exposes platform behavior.

The published recording set is:

| Clip | Scenario |
| --- | --- |
| `01-happy-path.mp4` | Complete build, test, registry, Helm deployment, and workload verification |
| `02-configuration-update.mp4` | Declarative configuration update and redeployment |
| `03-healthcheck-failure.mp4` | Controlled healthcheck failure and recovery |
| `04-crashloopbackoff.mp4` | Controlled startup failure and Kubernetes diagnostics |
| `05-k8s-self-healing.mp4` | Pod deletion and automatic replacement |
| `06-cleanup.mp4` | Helm resource cleanup and retained control-plane history |

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
- the project uses the Kubernetes runtime and the platform reports Helm deployment mode;
- the operator API token is kept for the current browser session in the UI if API authentication is enabled;
- any existing deployment history shown during the take is intentional and supports the scenario;
- no terminal, browser tab, or editor view exposes `.env.secrets`, tokens, passwords, or Secret values.

Use these healthy workload variables:

```text
APP_ENV=demo
FEATURE_MESSAGE=Running through the local PaaS
DEMO_STARTUP_DELAY_SECONDS=0
DEMO_FAIL_STARTUP=false
DEMO_HEALTH_STATUS=healthy
DEMO_RESPONSE_DELAY_MS=0
```

If `DEMO_SECRET` is part of the recording, select the **Secret key** source and provide only its Kubernetes resource name and key. Do not use or show a literal secret.

`APP_VERSION` and `APP_COMMIT_SHA` are reserved platform-owned variables. Do not configure literal values for them in the project. The running workload receives the unique build tag and exact build commit SHA automatically.

## Main Recording — 3–5 Minutes

### 1. Frame the project — 20 seconds

Open the Overview and say:

> This is a local operator-facing PaaS control plane. The API persists desired work, the worker builds images and pins registry-backed deployments by digest, and MicroK8s runs the workload. It is deliberately scoped as a portfolio system rather than a public multi-tenant platform.

Point out:

- API/platform readiness;
- active executor: `kubernetes`;
- API authentication posture;
- the Observability panel: structured logs, request correlation, metrics posture, and operational activity.

### 2. Show the workload contract — 25 seconds

Open the sample workload project and briefly show:

- GitHub repository and symbolic token reference;
- Dockerfile build context;
- port `5000`;
- healthcheck `/health`;
- environment variables and Secret reference support;
- CPU/memory fields and selected runtime.

Say that CPU and memory become Kubernetes requests, and that the selected health endpoint configures both readiness and liveness in either Kubernetes deployment mode.

Do not spend time editing every field. The point is that application configuration is declarative and separate from control-plane configuration.

### 3. Run a happy-path deployment — 60–90 seconds

Trigger **Run deploy / test**.

Before the pending deployment appears, say that the API resolves the selected branch head to an exact commit SHA. If that lookup fails, the request returns `409` and does not queue work. Then narrate the persisted event sequence as it appears:

1. worker clones the exact commit resolved by the API;
2. build the Docker image with BuildKit;
3. run the configured test command;
4. tag each build uniquely, push it, and retain the reported digest;
5. verify the remote image with `docker buildx imagetools inspect`;
6. validate referenced ConfigMaps and Secrets;
7. install or upgrade the Helm-managed Kubernetes workload;
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

Return to the PaaS and point out the selected deployment’s live health result. Explain that this probe originates from the control-plane API, while **Open service in browser** verifies the operator's browser path. Clarify that initial rollout health is persisted, while this post-deploy signal is transient and polled separately.

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

Open the workload again to show the changed hostname, or inspect the replacement with `kubectl get pods`. Keep the original Pod evidence in Diagnostics visible and point out its capture time. Refreshing Diagnostics only rereads this persisted snapshot; it does not collect the new Pod.

Say:

> The health signal is intentionally separate from lifecycle state. A transient Pod replacement should recover, not permanently rewrite deployment history as failed.

### 6. Close with diagnostics and cleanup — 30 seconds

Show:

- structured Pod fields: phase, container reason, restart count, images, and imagePullSecrets;
- build/runtime log tails;
- **Copy bundle** or **Download bundle**;
- **Cleanup Kubernetes resources** and explain that runtime resources are removed while build, event, audit, and diagnostic history is retained.

Close with:

> The project demonstrates the control-plane boundaries end to end: authenticated configuration, digest-pinned registry deployment flow, Kubernetes deployment, health and failure diagnostics, reconciliation, observability, and safe cleanup.

When discussing retention, show a failed deployment whose raw log remains available after reconciliation. Preview `cleanup-observability` in dry-run mode, then explain that applying the age-based policy removes eligible workspace files and changes the Summary log state to “removed by the retention policy.” Do not describe runtime cleanup and observability retention as the same operation.

## Focused Scenario Recordings

Record these separately rather than forcing every state transition into the main walkthrough. Keep the filenames from the recording-set table so the public walkthrough does not need to change when a clip is replaced.

### Configuration update

Change:

```text
FEATURE_MESSAGE=Configuration update deployed successfully
```

Save and redeploy. Show that the new workload receipt reflects the updated values and that deployment history retains the previous release.

### Historical retry versus redeploy

Start from a completed deployment and note its build tag and commit. Change a project-controlled variable such as `FEATURE_MESSAGE` and the project default test command, but do not overwrite the selected historical record.

Select the earlier deployment and queue **Retry failed** (a controlled failed attempt is appropriate for the UI action). Show in Summary:

- `Requested as: retry of #<source>`;
- `Inputs: Historical snapshot`;
- the same commit SHA as the source attempt;
- the workload retaining the retry's newly generated build tag, the original commit SHA, and the original effective test choice.

Then choose **Redeploy latest**. Show `Requested as: redeploy of #<source>`, `Inputs: Current project`, the branch's newly resolved head, the platform-injected build tag, and the current default test command. If the branch did not move during recording, say that the new HEAD happens to equal the earlier commit; the configuration source still differs.

Caption the limit precisely: “Retry repeats persisted inputs. Referenced secrets, external dependencies, registry/cluster state, and platform code remain current.” Never show Secret values or credentials.

### Kubernetes workload contract

Use a project with `cpu=250m`, `memory=512Mi`, and `healthcheck_path=/health`. Deploy it once in manifest mode and once in Helm mode, then inspect each exact Deployment name from Kubernetes Diagnostics:

```bash
kubectl get deployment <deployment-name> --namespace default -o yaml
```

Show the `app` container's `resources.requests`, named `http` port, and readiness/liveness probes. Both modes must show `250m`, `512Mi`, `/health`, readiness `5/10` seconds, and liveness `15/20` seconds. State that CPU/memory limits and startup probes are not part of the project contract. Do not claim that the existing Helm-focused clips demonstrate manifest mode; record this as an additional validation segment when needed.

### Healthcheck failure and recovery

Set:

```text
DEMO_HEALTH_STATUS=failed
```

Deploy and show the Helm readiness failure, events, and available diagnostic snapshot. Helm may time out during `--wait` before AutoDeploy reaches its separate Service port-forward HTTP probe. Remove most inactive waiting from the clip. Restore `healthy` and deploy again to demonstrate recovery as a new history record.

### CrashLoopBackOff and previous logs

The existing clip 04 predates the Helm snapshot remediation. In its replacement, show the original Helm error, persisted snapshot status/time, container state, restart count, available current or previous logs, and recovery. Keep the existing filename when publishing the replacement.

Set:

```text
DEMO_FAIL_STARTUP=true
```

Deploy and show:

- rollout failure;
- snapshot collection time and `complete`/`partial`/`unavailable` status;
- container reason/restart count;
- **Current logs** and **Previous logs** in Diagnostics when available;
- likely-cause guidance backed by container state or logs;
- a downloaded diagnostic bundle retaining the failed attempt evidence.

Keep the original Helm error visible. Explain missing previous logs or collection errors honestly; the Runtime log panel may remain empty for a failed install.

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

Use the PaaS cleanup action for an obsolete manifest deployment, or select the newest Helm deployment when intentionally removing its current shared release. A cleanup or stop command against a historical Helm record is correctly shown as skipped. Delete or scale a Deployment only when intentionally stopping a workload; deleting a managed Pod alone triggers Kubernetes self-healing.

## Recording Notes

- Keep one terminal for safe `kubectl` commands and one browser window for the PaaS/workload.
- Avoid showing long build output; use the event timeline and summaries.
- Keep one controlled failure in history because it makes diagnostics and recovery visible.
- Use exact Pod names for deletion.
- Never display local environment files, Docker Hub tokens, GitHub tokens, bearer tokens, kubeconfig contents, or Kubernetes Secret values.


For the image identity guarantee, show the persisted `repository@sha256:...` reference, a controlled tag replacement in a dedicated validation project, and a newly created Pod that still uses the original reference. Existing clips do not demonstrate this tag-mutation check.
