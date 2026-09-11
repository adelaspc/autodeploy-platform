# AutoDeploy Demo

This walkthrough demonstrates AutoDeploy as an operator-facing control plane rather than presenting the sample application as the product. Each scenario changes one deliberate input and follows the resulting build, deployment, health, event, and diagnostic evidence through the platform.

The reference workload is [Deployment Lab](https://github.com/adelaspc/paas-demo-app). It is a small, stateless Flask and Vue application designed to expose which release and configuration are actually running without reproducing control-plane state inside the workload.

## What the demo proves

- Project configuration is declarative and separated from control-plane configuration.
- HTTP requests persist desired work while a leased worker owns build and deployment side effects.
- Images are built, tested, uniquely tagged per build, pushed, and verified by their reported digest before registry-backed rollout.
- Kubernetes resources are installed through Helm and observed through rollout, health, logs, and diagnostics.
- ConfigMap and Secret references reach the workload without storing secret values in the project specification.
- Deployment history remains immutable across configuration changes, failures, recovery, and cleanup.

## Demo environment

The recording uses the `local-kubernetes` profile with the control plane and MySQL running in Docker Compose. The worker builds through the trusted local Docker socket, publishes to Docker Hub, and deploys the workload into a local MicroK8s namespace through Helm.

| Setting | Demo value | Why it is used |
| --- | --- | --- |
| Executor | `kubernetes` | Exercises the real Kubernetes deployment path. |
| Deployment mode | `helm` | Demonstrates release metadata, upgrade behavior, diagnostics, and cleanup. |
| Namespace | `default` | Keeps the local single-node demo easy to inspect. |
| Registry | `docker.io/adelicious` | Publishes an image that MicroK8s can pull. |
| Ingress | enabled with a local `nip.io` domain | Gives each deployment a browser-accessible service URL. |
| Image pull Secret | `dockerhub-pull` | Keeps registry credentials outside project data. |

Credentials remain in ignored local configuration or Kubernetes Secrets. The published walkthrough exposes symbolic references and resource names, never token, password, kubeconfig, or Secret values.

The console presents the selected project's recent activity and deployment history. API filters, cursors, and cross-project search are not exposed in this UI, and the top bar is not a search control.

## 00 — Project setup

**[Watch the project configuration walkthrough (MP4, 1.4 MB)](assets/demo/00-project-setup.mp4)**

**Goal:** Define the source, build, health, resource, and runtime configuration used by the deployment scenarios.

**Action:** Create the reference project, configure its symbolic Git credential reference, and add literal, ConfigMap-backed, and Secret-backed environment variables.

**What to watch:** Git credentials are referenced by name rather than displayed, Kubernetes configuration uses ConfigMap and Secret references, and no secret value is entered or exposed.

**Result:** The validated project is saved and selected, but no deployment has started yet.

### Project configuration

| Project field | Value | Purpose |
| --- | --- | --- |
| Name | `paas-demo-app` | Produces recognizable image and Helm release names. |
| Repository | `https://github.com/adelaspc/paas-demo-app.git` | Provides the Dockerfile-based reference workload. |
| Branch | `main` | Uses the published demo branch. |
| Git authentication | token reference `GITHUB` | Demonstrates symbolic Git credentials without persisting the token. |
| Dockerfile / build context | `Dockerfile` / `.` | Builds the repository root with its multi-stage image. |
| Port | `5000` | Matches the Gunicorn HTTP listener. |
| Healthcheck | `/health` | Exposes healthy, starting, and controlled failure states. |
| Test command | `python -m compileall -q backend` | Runs a deterministic validation inside the built runtime image. |
| CPU / memory | `250m` / `512Mi` | Demonstrates Kubernetes resource configuration. |
| Trigger | `manual` | Makes each deployment an explicit operator action. |

The migration command is intentionally empty. It is reserved by the v1 application specification and is not executed for user workloads.

### Runtime environment

| Variable | Source | Initial value or reference | What it demonstrates |
| --- | --- | --- | --- |
| `APP_ENV` | ConfigMap key | `demo-app-config` / `app-env` | Kubernetes-native non-secret configuration. |
| `APP_VERSION` | platform-injected | unique build image tag | Visible release identity for the workload. |
| `APP_COMMIT_SHA` | platform-injected | exact build commit SHA | Comparison between source identity and the running workload. |
| `FEATURE_MESSAGE` | literal | `Running through the local PaaS` | A visible configuration update. |
| `DEMO_SECRET` | Secret key | `demo-app-secret` / `demo-secret` | Secret presence without returning its value. |
| `DEMO_STARTUP_DELAY_SECONDS` | literal | `0` | Controlled readiness delay. |
| `DEMO_FAIL_STARTUP` | literal | `false` | Controlled CrashLoopBackOff scenario. |
| `DEMO_HEALTH_STATUS` | literal | `healthy` | Controlled healthcheck behavior. |
| `DEMO_RESPONSE_DELAY_MS` | literal | `0` | Optional application API latency. |

## 01 — Happy-path deployment

**[Watch with playback controls (MP4, 4.5 MB)](assets/demo/01-happy-path.mp4)**

**Goal:** Show the complete asynchronous path from a saved project to a healthy workload.

**Action:** Select the project configured in scenario 00 and choose **Run deploy / test**.

**What to watch:** The recording starts with the Kubernetes executor ready, then shows the request being persisted and queued before a background worker claims it. The deployment status and event stream expose source resolution, clone, image build, containerized test, registry push and digest verification, Kubernetes preflight, Helm rollout, and the transition to `running`. Build/runtime logs and Kubernetes diagnostics provide supporting evidence after the rollout.

**Result:** The final part opens the deployed service and compares its release identity, commit, environment, Pod hostname, secret-mounted state, and health posture with the persisted deployment summary. Secret presence is confirmed without displaying its value.

## 02 — CrashLoopBackOff diagnostics

**[Watch the CrashLoopBackOff diagnostics and recovery (MP4, 6.4 MB)](assets/demo/02-crash-loop-backoff.mp4)**

**Goal:** Show actionable container failure diagnostics.

**Change:** Starting from the healthy deployment, set `DEMO_FAIL_STARTUP=true`, save the project, and deploy. The reference application then raises a controlled exception during startup, causing Kubernetes to restart its container while Helm waits for readiness.

**What to watch:** The event timeline records successful image build, test, registry push, and digest verification before the Helm rollout fails. The persisted diagnostic snapshot is marked `partial`, preserves the original Helm error, identifies `CrashLoopBackOff`, reports the restart count, and includes previous container logs that identify the controlled startup failure. Supporting Kubernetes descriptions and events remain available in the same snapshot.

**Build status semantics:** The Summary shows `Build: failed` because the current lifecycle model marks the associated Build record failed whenever the overall pipeline terminates unsuccessfully, including during Helm deployment. For this attempt, the persisted stage events establish that image build, test, push, and remote verification completed before the rollout failure. The aggregate Build status is therefore not stage-specific build evidence.

**Recovery:** Restore `DEMO_FAIL_STARTUP=false` and start a new deployment. The new attempt reaches `running`, while the failed deployment and its captured evidence remain in history.

## 03 — Kubernetes self-healing

**[Watch Kubernetes replace a deleted Pod (MP4, 1.8 MB)](assets/demo/03-k8s-self-healing.mp4)**

**Goal:** Separate transient live health from persisted deployment lifecycle state.

**Action:** Starting from the healthy recovery deployment in scenario 02, note the current Pod identity, delete only that exact Pod with `kubectl`, and observe the Deployment controller create its replacement.

**What to watch:** The terminal output shows the original Pod terminating and a differently named Pod becoming ready. AutoDeploy does not create another deployment record or mark the selected deployment failed; its lifecycle remains `running`, and the control-plane API reports the replacement workload healthy. The workload receipt confirms the new Pod hostname while retaining the same release and commit identity.

**Health and diagnostics note:** Pod replacement can complete between the UI's live-health polls, so the recording does not promise that every replacement produces a visible `unhealthy` transition. Kubernetes Diagnostics remains the persisted snapshot captured for the deployment attempt; refreshing it rereads that evidence rather than inspecting the replacement Pod live.

## 04 — Diagnostics and cleanup

**[Watch persisted diagnostics and Helm cleanup (MP4, 5.0 MB)](assets/demo/04-diagnostics-and-cleanup.mp4)**

**Goal:** Close the lifecycle while retaining operator evidence.

**Action:** Revisit the failed CrashLoopBackOff attempt and its persisted evidence, then select the newest healthy deployment that owns the shared Helm release and choose **Cleanup Kubernetes resources**.

**What to watch:** The cleanup request is persisted and processed asynchronously by the worker. The event timeline records the Helm uninstall and successful cleanup, the selected deployment transitions to `stopped`, and its service link is removed. Deployment history, events, logs, and the earlier failure snapshot remain available. The final `kubectl get pods` and `microk8s helm list` outputs confirm that the namespace contains neither workload Pods nor the Helm release.

**Diagnostics export:** The Diagnostics panel provides **Copy bundle** and **Download bundle**. Exported bundles can contain application-produced output or operational data and must be reviewed before sharing.

## Reference workload baseline

The healthy scenarios use these values. Controlled-failure scenarios change one value and restore it through a new deployment:

```text
FEATURE_MESSAGE=Running through the local PaaS
DEMO_STARTUP_DELAY_SECONDS=0
DEMO_FAIL_STARTUP=false
DEMO_HEALTH_STATUS=healthy
DEMO_RESPONSE_DELAY_MS=0
```

For environment setup and operational troubleshooting, see the [runbook](runbook.md).
