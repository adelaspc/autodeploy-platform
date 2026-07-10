<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiRequest, storeToken, storedToken } from "./api";
import { buildDiagnosticsView } from "./diagnostics";
import { diagnosticsBundleFilename, diagnosticsBundleText } from "./diagnosticsBundle";
import { canCleanupDeployment } from "./serviceReachability";

const EMPTY_PROJECT_FORM = {
  name: "",
  repo_url: "",
  branch: "main",
  git_auth_type: "none",
  git_secret_ref: "",
  dockerfile_path: "Dockerfile",
  build_context: ".",
  port: 5000,
  healthcheck_path: "/health",
  default_test_command: "",
  migration_command: "",
  cpu: "",
  memory: "",
  trigger: "manual",
  runtime: "dockerfile",
  env_vars: [],
};

const projects = ref([]);
const selectedProjectId = ref(null);
const platformHealth = ref(null);
const platformActivity = ref(null);
const observabilityHealth = ref(null);
const projectStatus = ref(null);
const projectActivity = ref(null);
const selectedDeployment = ref(null);
const deploymentSummary = ref(null);
const deploymentEvents = ref([]);
const buildLog = ref(null);
const runtimeLog = ref(null);
const diagnostics = ref(null);
const apiToken = ref(storedToken());
const globalError = ref("");
const globalMessage = ref("");
const isLoading = ref(false);
const isDeploying = ref(false);
const isSavingProject = ref(false);
const isDeletingProject = ref(false);
const isCleaningDeployment = ref(false);
const serviceReachability = ref(null);
const formMode = ref("create");
const projectForm = ref(blankProjectForm());
const branchOverride = ref("");
const testCommandOverride = ref("");
const logTailLines = ref(200);
let liveHealthTimer = null;
let liveHealthRequestInFlight = false;
const LIVE_HEALTH_INTERVAL_MS = 2000;

const selectedProject = computed(() =>
  projects.value.find((project) => project.id === selectedProjectId.value) || null,
);
const latestDeployments = computed(() => projectActivity.value?.latest_deployments || []);
const activeDeployment = computed(() => projectStatus.value?.active_deployment || null);
const latestFailedDeployment = computed(() => projectStatus.value?.latest_failed_deployment || null);
const latestDeployment = computed(() => projectStatus.value?.latest_deployment || null);
const diagnosticsView = computed(() => buildDiagnosticsView(diagnostics.value));
const deployButtonLabel = computed(() => (isDeploying.value ? "Queuing" : "Run deploy / test"));
const projectSubmitLabel = computed(() => {
  if (isSavingProject.value) {
    return "Saving";
  }
  return formMode.value === "edit" ? "Save project" : "Create project";
});
const deploymentCreationReady = computed(() => platformHealth.value?.deployment_creation_ready !== false);

function blankProjectForm() {
  return structuredClone(EMPTY_PROJECT_FORM);
}

function setNotice(message) {
  globalMessage.value = message;
  globalError.value = "";
}

function setError(error) {
  globalError.value = error?.message || String(error);
  globalMessage.value = "";
}

async function initialize() {
  isLoading.value = true;
  globalError.value = "";
  try {
    await Promise.all([loadPlatform(), loadProjects()]);
    if (!selectedProjectId.value && projects.value.length) {
      await selectProject(projects.value[0].id);
    } else if (selectedProjectId.value) {
      await refreshProject();
    }
  } catch (error) {
    setError(error);
  } finally {
    isLoading.value = false;
  }
}

async function loadPlatform() {
  const [health, activity, observability] = await Promise.all([
    apiRequest("/health/platform"),
    apiRequest("/health/activity?latest_limit=8&active_limit=8&failed_limit=8"),
    apiRequest("/health/observability"),
  ]);
  platformHealth.value = health;
  platformActivity.value = activity;
  observabilityHealth.value = observability;
}

async function loadProjects() {
  projects.value = await apiRequest("/api/projects");
  if (selectedProjectId.value && !projects.value.some((project) => project.id === selectedProjectId.value)) {
    selectedProjectId.value = null;
  }
}

async function selectProject(projectId) {
  stopLiveHealthMonitor();
  selectedProjectId.value = projectId;
  selectedDeployment.value = null;
  deploymentSummary.value = null;
  deploymentEvents.value = [];
  buildLog.value = null;
  runtimeLog.value = null;
  diagnostics.value = null;
  branchOverride.value = selectedProject.value?.branch || "";
  testCommandOverride.value = selectedProject.value?.default_test_command || "";
  editSelectedProject();
  await refreshProject();
}

async function refreshProject() {
  if (!selectedProjectId.value) {
    projectStatus.value = null;
    projectActivity.value = null;
    return;
  }
  globalError.value = "";
  try {
    const [status, activity] = await Promise.all([
      apiRequest(`/api/projects/${selectedProjectId.value}/status`),
      apiRequest(`/api/projects/${selectedProjectId.value}/activity?latest_limit=10&webhook_limit=5`),
    ]);
    projectStatus.value = status;
    projectActivity.value = activity;
    if (selectedDeployment.value) {
      await selectDeployment(selectedDeployment.value);
    } else if (latestDeployment.value) {
      await selectDeployment(latestDeployment.value);
    }
  } catch (error) {
    setError(error);
  }
}

function newProject() {
  formMode.value = "create";
  projectForm.value = blankProjectForm();
}

function editSelectedProject() {
  if (!selectedProject.value) {
    newProject();
    return;
  }
  formMode.value = "edit";
  projectForm.value = projectToForm(selectedProject.value);
}

function projectToForm(project) {
  return {
    name: project.name || "",
    repo_url: project.repo_url || "",
    branch: project.branch || "main",
    git_auth_type: project.git_auth_type || "none",
    git_secret_ref: project.git_secret_ref || "",
    dockerfile_path: project.dockerfile_path || "Dockerfile",
    build_context: project.build_context || ".",
    port: project.port || 5000,
    healthcheck_path: project.healthcheck_path || "/health",
    default_test_command: project.default_test_command || "",
    migration_command: project.migration_command || "",
    cpu: project.cpu || "",
    memory: project.memory || "",
    trigger: project.trigger || "manual",
    runtime: project.runtime || "dockerfile",
    env_vars: (project.env_vars || []).map(envVarToFormRow),
  };
}

function envVarToFormRow(item) {
  const valueSource = item.value_source || (item.value !== undefined && item.value !== null ? "literal" : "literal");
  return {
    name: item.name || "",
    value_source: valueSource,
    value: item.is_secret && item.value === "[REDACTED]" ? "" : item.value || "",
    is_secret: Boolean(item.is_secret),
    source_name: item.source_name || "",
    source_key: item.source_key || "",
  };
}

function addEnvVar() {
  projectForm.value.env_vars.push({
    name: "",
    value_source: "literal",
    value: "",
    is_secret: false,
    source_name: "",
    source_key: "",
  });
}

function removeEnvVar(index) {
  projectForm.value.env_vars.splice(index, 1);
}

function normalizeEnvRowForSubmit(row, index) {
  const name = row.name.trim();
  if (!name) {
    throw new Error(`Env var ${index + 1} needs a name`);
  }

  if (row.value_source === "literal") {
    if (!row.value) {
      throw new Error(`Env var ${name} needs a literal value`);
    }
    return {
      name,
      value_source: "literal",
      value: row.value,
      is_secret: Boolean(row.is_secret),
    };
  }

  if (!row.source_name.trim() || !row.source_key.trim()) {
    throw new Error(`Env var ${name} needs source name and source key`);
  }
  return {
    name,
    value_source: row.value_source,
    source_name: row.source_name.trim(),
    source_key: row.source_key.trim(),
    is_secret: row.value_source === "secret_key_ref" ? true : Boolean(row.is_secret),
  };
}

function projectPayload() {
  const form = projectForm.value;
  return {
    name: form.name.trim(),
    repo_url: form.repo_url.trim(),
    branch: form.branch.trim(),
    git_auth_type: form.git_auth_type,
    git_secret_ref: form.git_auth_type === "token" ? form.git_secret_ref.trim() : null,
    dockerfile_path: form.dockerfile_path.trim() || "Dockerfile",
    build_context: form.build_context.trim() || ".",
    port: Number(form.port),
    healthcheck_path: form.healthcheck_path.trim(),
    env_vars: form.env_vars.map(normalizeEnvRowForSubmit),
    default_test_command: form.default_test_command.trim() || null,
    migration_command: form.migration_command.trim() || null,
    cpu: form.cpu.trim() || null,
    memory: form.memory.trim() || null,
    trigger: form.trigger,
    runtime: form.runtime,
  };
}

async function saveProject() {
  isSavingProject.value = true;
  try {
    const payload = projectPayload();
    const saved =
      formMode.value === "edit" && selectedProjectId.value
        ? await apiRequest(`/api/projects/${selectedProjectId.value}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : await apiRequest("/api/projects", {
            method: "POST",
            body: JSON.stringify(payload),
          });
    await loadProjects();
    await selectProject(saved.id);
    setNotice(formMode.value === "edit" ? "Project saved" : "Project created");
  } catch (error) {
    setError(error);
  } finally {
    isSavingProject.value = false;
  }
}

async function deleteSelectedProject() {
  if (!selectedProjectId.value || !selectedProject.value) {
    return;
  }
  isDeletingProject.value = true;
  try {
    await apiRequest(`/api/projects/${selectedProjectId.value}`, { method: "DELETE" });
    setNotice(`Deleted project ${selectedProject.value.name}`);
    selectedProjectId.value = null;
    selectedDeployment.value = null;
    deploymentSummary.value = null;
    projectStatus.value = null;
    projectActivity.value = null;
    newProject();
    await loadProjects();
    if (projects.value.length) {
      await selectProject(projects.value[0].id);
    }
  } catch (error) {
    setError(error);
  } finally {
    isDeletingProject.value = false;
  }
}

async function triggerDeploy() {
  if (!selectedProjectId.value) {
    return;
  }
  isDeploying.value = true;
  globalError.value = "";
  const body = {};
  if (branchOverride.value.trim() && branchOverride.value.trim() !== selectedProject.value?.branch) {
    body.branch = branchOverride.value.trim();
  }
  if (testCommandOverride.value.trim() !== (selectedProject.value?.default_test_command || "")) {
    body.test_command = testCommandOverride.value.trim();
  }
  try {
    const deployment = await apiRequest(`/api/projects/${selectedProjectId.value}/deploy`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    setNotice(`Queued deployment #${deployment.deployment_id}`);
    await Promise.all([loadPlatform(), refreshProject()]);
    await selectDeployment({
      deployment_id: deployment.deployment_id,
      project_id: deployment.project_id,
      status: deployment.status,
      build_status: "pending",
      commit_sha: deployment.commit_sha,
      image_ref: deployment.image_ref,
      created_at: new Date().toISOString(),
    });
  } catch (error) {
    setError(error);
  } finally {
    isDeploying.value = false;
  }
}

async function retryDeployment(deployment) {
  if (!deployment?.deployment_id || !selectedProjectId.value) {
    return;
  }
  await runDeploymentAction(
    `/api/projects/${selectedProjectId.value}/deployments/${deployment.deployment_id}/retry`,
    "POST",
    null,
    "Retry queued",
  );
}

async function redeployLatest() {
  if (!selectedProjectId.value) {
    return;
  }
  await runDeploymentAction(`/api/projects/${selectedProjectId.value}/redeploy`, "POST", null, "Redeploy queued");
}

async function stopDeployment(deployment) {
  if (!deployment?.deployment_id || !selectedProjectId.value) {
    return;
  }
  await runDeploymentAction(
    `/api/projects/${selectedProjectId.value}/deployments/${deployment.deployment_id}/stop`,
    "POST",
    null,
    "Stop requested",
  );
}

async function cleanupDeployment(deployment) {
  if (!deployment?.deployment_id || !selectedProjectId.value || isCleaningDeployment.value) return;
  if (!window.confirm(`Delete Kubernetes resources for deployment #${deployment.deployment_id}?`)) return;
  isCleaningDeployment.value = true;
  try {
    await runDeploymentAction(
      `/api/projects/${selectedProjectId.value}/deployments/${deployment.deployment_id}/cleanup`,
      "POST",
      null,
      "Kubernetes resources cleaned up",
    );
  } finally {
    isCleaningDeployment.value = false;
  }
}

async function runDeploymentAction(path, method, payload = null, successMessage = "Action completed") {
  globalError.value = "";
  try {
    const response = await apiRequest(path, {
      method,
      body: payload ? JSON.stringify(payload) : undefined,
    });
    setNotice(successMessage);
    await Promise.all([loadPlatform(), refreshProject()]);
    const deploymentId = response?.deployment_id || response?.id;
    if (deploymentId) {
      await selectDeployment({ deployment_id: deploymentId, project_id: selectedProjectId.value });
    }
  } catch (error) {
    setError(error);
  }
}

async function selectDeployment(deployment) {
  if (!deployment?.deployment_id || !selectedProjectId.value) {
    return;
  }
  selectedDeployment.value = deployment;
  deploymentSummary.value = null;
  deploymentEvents.value = [];
  buildLog.value = null;
  runtimeLog.value = null;
  diagnostics.value = null;
  serviceReachability.value = null;
  stopLiveHealthMonitor();
  try {
    const deploymentId = deployment.deployment_id;
    const summary = await apiRequest(`/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/summary`);
    const events = await apiRequest(`/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/events`);
    deploymentSummary.value = summary;
    if (summary.service_url && (summary.deploy_target !== "kubernetes" || summary.kubernetes_ingress_host)) {
      await checkSelectedDeploymentHealth();
      startLiveHealthMonitor();
    }
    deploymentEvents.value = events;
    selectedDeployment.value = {
      ...deployment,
      deployment_id: summary.deployment_id,
      status: summary.deployment_status,
      build_status: summary.build_status,
      deploy_target: summary.deploy_target,
      service_url: summary.service_url,
      commit_sha: summary.commit_sha,
      image_ref: summary.image_ref,
      created_at: summary.created_at,
      updated_at: summary.updated_at,
    };
    await loadLogsAndDiagnostics(summary);
  } catch (error) {
    setError(error);
  }
}

async function checkSelectedDeploymentHealth() {
  const summary = deploymentSummary.value;
  if (!summary || !selectedProjectId.value || liveHealthRequestInFlight) {
    return;
  }
  if (summary.deployment_status !== "running") {
    serviceReachability.value = null;
    return;
  }
  liveHealthRequestInFlight = true;
  if (!serviceReachability.value) {
    serviceReachability.value = { status: "checking", message: "Checking workload health..." };
  }
  try {
    serviceReachability.value = await apiRequest(
      `/api/projects/${selectedProjectId.value}/deployments/${summary.deployment_id}/live-health`,
    );
  } catch (error) {
    serviceReachability.value = {
      status: "unavailable",
      message: `Health monitor unavailable: ${error.message}`,
      checked_at: new Date().toISOString(),
    };
  } finally {
    liveHealthRequestInFlight = false;
  }
}

function startLiveHealthMonitor() {
  stopLiveHealthMonitor();
  if (deploymentSummary.value?.deployment_status === "running") {
    liveHealthTimer = window.setInterval(checkSelectedDeploymentHealth, LIVE_HEALTH_INTERVAL_MS);
  }
}

function stopLiveHealthMonitor() {
  if (liveHealthTimer !== null) {
    window.clearInterval(liveHealthTimer);
    liveHealthTimer = null;
  }
}

async function loadLogsAndDiagnostics(summary = deploymentSummary.value) {
  if (!summary || !selectedProjectId.value) {
    return;
  }
  try {
    const deploymentId = summary.deployment_id;
    buildLog.value = await optionalRequest(
      `/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/build-log?tail_lines=${logTailLines.value}`,
    );
    runtimeLog.value = await optionalRequest(
      `/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/runtime-log?tail_lines=${logTailLines.value}`,
    );
    diagnostics.value =
      summary.deploy_target === "kubernetes"
        ? await optionalRequest(`/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/kubernetes-diagnostics`)
        : null;
  } catch (error) {
    setError(error);
  }
}

function currentDiagnosticsBundleText() {
  return diagnosticsBundleText({
    summary: deploymentSummary.value,
    diagnostics: diagnostics.value,
    events: deploymentEvents.value,
    buildLog: buildLog.value,
    runtimeLog: runtimeLog.value,
  });
}

async function copyDiagnosticsBundle() {
  try {
    await navigator.clipboard.writeText(currentDiagnosticsBundleText());
    setNotice("Diagnostics bundle copied");
  } catch (error) {
    setError(new Error(`Unable to copy diagnostics bundle: ${error?.message || error}`));
  }
}

function downloadDiagnosticsBundle() {
  const blob = new Blob([currentDiagnosticsBundleText()], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = diagnosticsBundleFilename(deploymentSummary.value);
  link.click();
  URL.revokeObjectURL(url);
  setNotice("Diagnostics bundle downloaded");
}

async function optionalRequest(path) {
  try {
    return await apiRequest(path);
  } catch (error) {
    if (error.status === 404) {
      return null;
    }
    throw error;
  }
}

function saveToken() {
  storeToken(apiToken.value);
  initialize();
}

function clearToken() {
  apiToken.value = "";
  storeToken("");
  initialize();
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString() : "not recorded";
}

function shortSha(value) {
  return value ? value.slice(0, 12) : "unknown";
}

function statusTone(status) {
  if (["ok", "running", "succeeded", "reachable", "healthy"].includes(status)) {
    return "success";
  }
  if (["pending", "cloning", "building", "testing", "pushing_image", "deploying"].includes(status)) {
    return "warning";
  }
  if (["failed", "degraded", "error"].includes(status)) {
    return "danger";
  }
  return "neutral";
}

function canStop(deployment) {
  return deployment?.status === "running";
}

function canRetry(deployment) {
  return deployment?.status === "failed";
}

function canCleanup(deployment) {
  return canCleanupDeployment(deployment);
}

onMounted(initialize);
onBeforeUnmount(stopLiveHealthMonitor);
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="#overview">
        <span class="prompt-mark">&gt;_</span>
        <span>PaaS Console</span>
      </a>
      <nav class="side-nav" aria-label="Dashboard sections">
        <a class="active" href="#overview"><span class="nav-icon">01</span>Overview</a>
        <a href="#projects"><span class="nav-icon">02</span>Projects</a>
        <a href="#deployments"><span class="nav-icon">03</span>Deployments</a>
        <a href="#events"><span class="nav-icon">04</span>Events</a>
        <a href="#logs"><span class="nav-icon">05</span>Logs</a>
        <a href="#diagnostics"><span class="nav-icon">06</span>Diagnostics</a>
        <a href="#observability"><span class="nav-icon">07</span>Observability</a>
      </nav>
      <div class="system-card">
        <span class="status-dot" :data-tone="statusTone(platformHealth?.status)"></span>
        <strong>{{ platformHealth?.status === "ok" ? "All Systems Operational" : "Platform Status" }}</strong>
        <small>{{ platformHealth?.executor || "executor unknown" }}</small>
      </div>
      <small class="version">local control plane</small>
    </aside>

    <main class="screen">
      <section class="topbar">
        <button class="icon-button" type="button" aria-label="Dashboard menu">=</button>
        <div class="search-shell">
          <span>Search apps, deployments, logs...</span>
          <kbd>/</kbd>
        </div>
        <form class="token-form" @submit.prevent="saveToken">
          <input v-model="apiToken" type="password" placeholder="Bearer token" autocomplete="off" />
          <button type="submit">Save</button>
          <button type="button" @click="clearToken">Clear</button>
        </form>
      </section>

      <section class="hero-strip" id="overview">
        <div>
          <p class="eyebrow">operator console</p>
          <h1>PaaS Control Plane</h1>
        </div>
        <button type="button" @click="initialize">{{ isLoading ? "Refreshing" : "Refresh" }}</button>
      </section>

      <p v-if="globalError" class="alert">Error: {{ globalError }}</p>
      <p v-if="globalMessage" class="notice">{{ globalMessage }}</p>

      <section class="status-grid">
        <article class="panel stat-card">
          <div class="panel-head">
            <span>api health</span>
            <span class="panel-glyph">API</span>
          </div>
          <strong class="stat-value" :data-tone="statusTone(platformHealth?.status)">
            {{ platformHealth?.status || "unknown" }}
          </strong>
          <small>{{ deploymentCreationReady ? "deployment creation ready" : "deployment creation blocked" }}</small>
          <p v-if="platformHealth?.deployment_creation_error" class="muted">
            {{ platformHealth.deployment_creation_error }}
          </p>
        </article>

        <article class="panel stat-card">
          <div class="panel-head">
            <span>active deployments</span>
            <span class="panel-glyph">RUN</span>
          </div>
          <strong class="stat-value" data-tone="warning">{{ platformActivity?.active_deployment_count ?? 0 }}</strong>
          <small>{{ platformActivity?.latest_deployment_at ? "latest activity recorded" : "idle" }}</small>
        </article>

        <article class="panel stat-card">
          <div class="panel-head">
            <span>failed deployments</span>
            <span class="panel-glyph">ERR</span>
          </div>
          <strong class="stat-value" :data-tone="(platformActivity?.failed_deployment_count ?? 0) ? 'danger' : 'success'">
            {{ platformActivity?.failed_deployment_count ?? 0 }}
          </strong>
          <small>{{ platformActivity?.recent_webhook_delivery_count ?? 0 }} recent webhooks</small>
        </article>

        <article class="panel stat-card">
          <div class="panel-head">
            <span>executor</span>
            <span class="panel-glyph">CPU</span>
          </div>
          <strong class="stat-value compact-value">{{ platformHealth?.executor || "unknown" }}</strong>
          <small>{{ deploymentCreationReady ? "ready" : "not ready" }}</small>
        </article>
      </section>

      <section class="observability-panel panel" id="observability">
        <div class="panel-head">
          <span>observability</span>
          <span class="subtle">lightweight operational signals</span>
        </div>
        <div class="observability-grid">
          <article class="observability-card">
            <span>Metrics endpoint</span>
            <strong :data-tone="observabilityHealth?.metrics?.enabled ? 'success' : 'neutral'">
              {{ observabilityHealth?.metrics?.enabled ? "enabled" : "disabled" }}
            </strong>
            <small>{{ observabilityHealth?.metrics?.endpoint || "/metrics" }} · dedicated bearer token</small>
          </article>
          <article class="observability-card">
            <span>Structured logs</span>
            <strong :data-tone="observabilityHealth?.logging?.structured ? 'success' : 'neutral'">
              {{ observabilityHealth?.logging?.format || "unknown" }}
            </strong>
            <small>{{ observabilityHealth?.logging?.destination || "stdout" }} · API / worker / reconciler</small>
          </article>
          <article class="observability-card">
            <span>Request correlation</span>
            <strong data-tone="success">enabled</strong>
            <small>{{ observabilityHealth?.request_correlation?.header || "X-Request-ID" }}</small>
          </article>
          <article class="observability-card">
            <span>Operational activity</span>
            <strong :data-tone="(platformActivity?.failed_deployment_count ?? 0) ? 'danger' : 'success'">
              {{ platformActivity?.active_deployment_count ?? 0 }} active / {{ platformActivity?.failed_deployment_count ?? 0 }} failed
            </strong>
            <small>events, audit, logs and diagnostics retained</small>
          </article>
        </div>
      </section>

      <section class="dashboard-grid">
        <article class="panel project-picker" id="projects">
          <div class="panel-head">
            <span>projects</span>
            <button type="button" @click="newProject">New</button>
          </div>
          <button
            v-for="project in projects"
            :key="project.id"
            class="project-button"
            :data-active="project.id === selectedProjectId"
            type="button"
            @click="selectProject(project.id)"
          >
            <span>{{ project.name }}</span>
            <small>{{ project.branch }} / {{ project.runtime }}</small>
          </button>
          <p v-if="!projects.length" class="muted">No projects returned by the API.</p>
        </article>

        <article class="panel deploy-panel" id="deployments">
          <div class="panel-head">
            <span>run deploy / test</span>
            <span v-if="selectedProject" class="subtle">project #{{ selectedProject.id }}</span>
          </div>
          <template v-if="selectedProject">
            <div class="project-summary">
              <h2>{{ selectedProject.name }}</h2>
              <p>{{ selectedProject.repo_url }}</p>
            </div>
            <form class="deploy-form" @submit.prevent="triggerDeploy">
              <label>
                <span>Branch</span>
                <input v-model.trim="branchOverride" placeholder="main" />
              </label>
              <label>
                <span>Test command</span>
                <input v-model="testCommandOverride" placeholder="pytest, npm test, or leave empty" />
              </label>
              <button type="submit" :disabled="isDeploying || !deploymentCreationReady">
                {{ deployButtonLabel }}
              </button>
            </form>
            <div class="actions-row">
              <button type="button" :disabled="!latestDeployment" @click="redeployLatest">Redeploy latest</button>
              <button type="button" :disabled="!canRetry(latestFailedDeployment)" @click="retryDeployment(latestFailedDeployment)">
                Retry failed
              </button>
              <button type="button" :disabled="!canStop(activeDeployment)" @click="stopDeployment(activeDeployment)">
                Stop running
              </button>
            </div>
          </template>
          <p v-else class="muted">Select a project to run deployments.</p>
        </article>
      </section>

      <section class="project-editor">
        <article class="panel">
        <div class="panel-head">
          <span>{{ formMode === "edit" ? "edit project" : "create project" }}</span>
          <button type="button" :disabled="!selectedProject" @click="editSelectedProject">Use selected</button>
        </div>
        <form class="project-form" @submit.prevent="saveProject">
          <label>
            <span>Name</span>
            <input v-model.trim="projectForm.name" required />
          </label>
          <label class="wide">
            <span>Repository URL</span>
            <input v-model.trim="projectForm.repo_url" required placeholder="https://github.com/user/repo.git" />
          </label>
          <label>
            <span>Branch</span>
            <input v-model.trim="projectForm.branch" required />
          </label>
          <label>
            <span>Port</span>
            <input v-model.number="projectForm.port" required type="number" min="1" max="65535" />
          </label>
          <label>
            <span>Dockerfile</span>
            <input v-model.trim="projectForm.dockerfile_path" required />
          </label>
          <label>
            <span>Build context</span>
            <input v-model.trim="projectForm.build_context" required />
          </label>
          <label>
            <span>Healthcheck path</span>
            <input v-model.trim="projectForm.healthcheck_path" required />
          </label>
          <label>
            <span>Default test command</span>
            <input v-model="projectForm.default_test_command" placeholder="pytest, npm test, or empty" />
          </label>
          <label>
            <span>Migration command</span>
            <input v-model="projectForm.migration_command" placeholder="optional" />
          </label>
          <label>
            <span>CPU</span>
            <input v-model.trim="projectForm.cpu" placeholder="250m" />
          </label>
          <label>
            <span>Memory</span>
            <input v-model.trim="projectForm.memory" placeholder="512Mi" />
          </label>
          <label>
            <span>Trigger</span>
            <select v-model="projectForm.trigger">
              <option value="manual">manual</option>
              <option value="github_push">github_push</option>
            </select>
          </label>
          <label>
            <span>Runtime</span>
            <select v-model="projectForm.runtime">
              <option value="dockerfile">dockerfile</option>
            </select>
          </label>
          <label>
            <span>Git auth</span>
            <select v-model="projectForm.git_auth_type">
              <option value="none">none</option>
              <option value="token">token</option>
            </select>
          </label>
          <label>
            <span>Git secret ref</span>
            <input v-model.trim="projectForm.git_secret_ref" :disabled="projectForm.git_auth_type !== 'token'" />
          </label>

          <div class="env-editor wide">
            <div class="panel-head inline-head">
              <span>environment variables</span>
              <button type="button" @click="addEnvVar">Add env var</button>
            </div>
            <div v-if="projectForm.env_vars.length" class="env-list">
              <div v-for="(envVar, index) in projectForm.env_vars" :key="index" class="env-row">
                <label>
                  <span>Name</span>
                  <input v-model.trim="envVar.name" />
                </label>
                <label>
                  <span>Source</span>
                  <select v-model="envVar.value_source">
                    <option value="literal">literal</option>
                    <option value="configmap_key_ref">configmap key</option>
                    <option value="secret_key_ref">secret key</option>
                  </select>
                </label>
                <label v-if="envVar.value_source === 'literal'">
                  <span>Value</span>
                  <input v-model="envVar.value" :type="envVar.is_secret ? 'password' : 'text'" />
                </label>
                <label v-else>
                  <span>Source name</span>
                  <input v-model.trim="envVar.source_name" />
                </label>
                <label v-if="envVar.value_source !== 'literal'">
                  <span>Source key</span>
                  <input v-model.trim="envVar.source_key" />
                </label>
                <label class="checkbox-label">
                  <input v-model="envVar.is_secret" type="checkbox" :disabled="envVar.value_source === 'secret_key_ref'" />
                  <span>secret</span>
                </label>
                <button type="button" @click="removeEnvVar(index)">Remove</button>
              </div>
            </div>
            <p v-else class="muted">No env vars configured.</p>
          </div>

          <div class="form-actions wide">
            <button type="submit" :disabled="isSavingProject">{{ projectSubmitLabel }}</button>
            <button type="button" @click="newProject">Reset</button>
            <button
              v-if="formMode === 'edit'"
              class="danger-button"
              type="button"
              :disabled="isDeletingProject"
              @click="deleteSelectedProject"
            >
              {{ isDeletingProject ? "Deleting" : "Delete project" }}
            </button>
          </div>
        </form>
      </article>
      </section>

      <section class="workspace">
      <article class="panel">
        <div class="panel-head">
          <span>deployment history</span>
          <button type="button" :disabled="!selectedProjectId" @click="refreshProject">Refresh</button>
        </div>
        <div class="deployment-list">
          <button
            v-for="deployment in latestDeployments"
            :key="deployment.deployment_id"
            class="deployment-row"
            :data-active="deployment.deployment_id === selectedDeployment?.deployment_id"
            type="button"
            @click="selectDeployment(deployment)"
          >
            <span>#{{ deployment.deployment_id }}</span>
            <strong :data-tone="statusTone(deployment.status)">{{ deployment.status }}</strong>
            <small>{{ shortSha(deployment.commit_sha) }}</small>
            <small>{{ formatTime(deployment.created_at) }}</small>
          </button>
          <p v-if="selectedProject && !latestDeployments.length" class="muted">No deployments for this project yet.</p>
        </div>
      </article>
    </section>

    <section class="details-grid">
      <article class="panel">
        <div class="panel-head">
          <span>summary</span>
          <span v-if="deploymentSummary" class="subtle">#{{ deploymentSummary.deployment_id }}</span>
        </div>
        <template v-if="deploymentSummary">
          <div class="metric-row">
            <span>Deployment</span>
            <strong :data-tone="statusTone(deploymentSummary.deployment_status)">
              {{ deploymentSummary.deployment_status }}
            </strong>
          </div>
          <div class="metric-row">
            <span>Build</span>
            <strong :data-tone="statusTone(deploymentSummary.build_status)">{{ deploymentSummary.build_status }}</strong>
          </div>
          <div class="metric-row">
            <span>Step</span>
            <strong>{{ deploymentSummary.current_step }}</strong>
          </div>
          <div class="metric-row">
            <span>Image</span>
            <strong>{{ deploymentSummary.image_ref || "not built" }}</strong>
          </div>
          <a
            v-if="
              deploymentSummary.service_url &&
              (deploymentSummary.deploy_target !== 'kubernetes' || deploymentSummary.kubernetes_ingress_host)
            "
            class="service-link"
            :href="deploymentSummary.service_url"
            target="_blank"
            rel="noopener noreferrer"
          >
            Open service
          </a>
          <div
            v-if="serviceReachability"
            class="live-health"
            :data-health="serviceReachability.status"
          >
            <span>Live health</span>
            <strong :data-tone="statusTone(serviceReachability.status)">{{ serviceReachability.status }}</strong>
            <small>{{ serviceReachability.message }}</small>
            <small v-if="serviceReachability.checked_at">checked {{ formatTime(serviceReachability.checked_at) }}</small>
          </div>
          <button
            v-if="canCleanup(deploymentSummary)"
            class="danger-button"
            type="button"
            :disabled="isCleaningDeployment"
            @click="cleanupDeployment(deploymentSummary)"
          >
            {{ isCleaningDeployment ? "Cleaning up" : "Cleanup Kubernetes resources" }}
          </button>
          <div
            v-if="
              deploymentSummary.internal_service_url ||
              (deploymentSummary.deploy_target === 'kubernetes' &&
                !deploymentSummary.kubernetes_ingress_host &&
                deploymentSummary.service_url)
            "
            class="metric-row"
          >
            <span>Internal service</span>
            <strong>{{ deploymentSummary.internal_service_url || deploymentSummary.service_url }}</strong>
          </div>
          <p v-if="deploymentSummary.last_error" class="alert compact">{{ deploymentSummary.last_error }}</p>
        </template>
        <p v-else class="muted">Select a deployment to inspect it.</p>
      </article>

      <article class="panel events-panel" id="events">
        <div class="panel-head">
          <span>events</span>
          <span class="subtle">{{ deploymentEvents.length }}</span>
        </div>
        <div class="event-list">
          <div v-for="event in deploymentEvents" :key="event.id" class="event-row">
            <span :data-tone="statusTone(event.status)">{{ event.status }}</span>
            <strong>{{ event.event_type }}</strong>
            <small>{{ event.step || "step unknown" }} - {{ formatTime(event.created_at) }}</small>
            <p v-if="event.message">{{ event.message }}</p>
          </div>
          <p v-if="!deploymentEvents.length" class="muted">No events loaded.</p>
        </div>
      </article>
    </section>

    <section class="log-grid" id="logs">
      <article class="panel log-panel">
        <div class="panel-head">
          <span>build log</span>
          <button type="button" :disabled="!deploymentSummary" @click="loadLogsAndDiagnostics()">Reload</button>
        </div>
        <pre v-if="buildLog?.content">{{ buildLog.content }}</pre>
        <p v-else class="muted">No build log available.</p>
      </article>

      <article class="panel log-panel">
        <div class="panel-head">
          <span>runtime log</span>
          <label class="tail-control">
            <span>tail</span>
            <input v-model.number="logTailLines" type="number" min="1" max="2000" @change="loadLogsAndDiagnostics()" />
          </label>
        </div>
        <pre v-if="runtimeLog?.content">{{ runtimeLog.content }}</pre>
        <p v-else class="muted">No runtime log available.</p>
      </article>
    </section>

    <section v-if="diagnostics" class="panel diagnostics-panel" id="diagnostics">
      <div class="panel-head">
        <span>kubernetes diagnostics</span>
        <div class="actions-row">
          <button type="button" @click="copyDiagnosticsBundle">Copy bundle</button>
          <button type="button" @click="downloadDiagnosticsBundle">Download bundle</button>
          <span class="subtle">{{ diagnosticsView.stageLabel }}</span>
        </div>
      </div>
      <div class="diagnostics-summary" :data-stage="diagnosticsView.stage || 'none'">
        <span>{{ diagnosticsView.stageLabel }}</span>
        <strong>{{ diagnosticsView.summary }}</strong>
        <small>
          {{ diagnosticsView.eventType }}
          <template v-if="diagnosticsView.eventAt"> - {{ formatTime(diagnosticsView.eventAt) }}</template>
        </small>
      </div>
      <div v-if="diagnosticsView.insights.length" class="diagnostics-insights">
        <article v-for="insight in diagnosticsView.insights" :key="insight.title" class="diagnostics-insight">
          <span>Likely cause</span>
          <strong>{{ insight.title }}</strong>
          <p>{{ insight.detail }}</p>
          <small>{{ insight.action }}</small>
        </article>
      </div>

      <div class="diagnostics-grid">
        <article v-if="diagnosticsView.hasHelm" class="diagnostics-section">
          <h2>Helm</h2>
          <div v-if="diagnosticsView.helmFields.length" class="diagnostics-fields">
            <div v-for="field in diagnosticsView.helmFields" :key="field.label" class="metric-row">
              <span>{{ field.label }}</span>
              <strong>{{ field.value }}</strong>
            </div>
          </div>
          <p v-if="diagnosticsView.helmStderrSummary" class="alert compact">{{ diagnosticsView.helmStderrSummary }}</p>
          <pre v-if="diagnosticsView.helmStdoutSummary" class="diagnostics-snippet">{{ diagnosticsView.helmStdoutSummary }}</pre>
        </article>

        <article v-if="diagnosticsView.hasResourceContext" class="diagnostics-section">
          <h2>Kubernetes</h2>
          <div v-if="diagnosticsView.podFields.length" class="diagnostics-fields">
            <div v-for="field in diagnosticsView.podFields" :key="field.label" class="metric-row">
              <span>{{ field.label }}</span>
              <strong>{{ field.value }}</strong>
            </div>
          </div>
          <div v-if="diagnosticsView.resourceFields.length" class="diagnostics-fields">
            <div v-for="field in diagnosticsView.resourceFields" :key="field.label" class="metric-row">
              <span>{{ field.label }}</span>
              <strong>{{ field.value }}</strong>
            </div>
          </div>
          <div v-if="diagnosticsView.missingResources.length" class="diagnostics-list">
            <span>Missing resources</span>
            <strong>{{ diagnosticsView.missingResources.join(", ") }}</strong>
          </div>
          <div v-if="diagnosticsView.checkedResources.length" class="diagnostics-list">
            <span>Checked resources</span>
            <strong>{{ diagnosticsView.checkedResources.join(", ") }}</strong>
          </div>
          <div v-if="diagnosticsView.podNames.length" class="diagnostics-list">
            <span>Pods</span>
            <strong>{{ diagnosticsView.podNames.join(", ") }}</strong>
          </div>
          <pre v-if="diagnosticsView.podDescribeSummary" class="diagnostics-snippet">{{ diagnosticsView.podDescribeSummary }}</pre>
          <pre v-if="diagnosticsView.podLogsSummary" class="diagnostics-snippet">{{ diagnosticsView.podLogsSummary }}</pre>
          <pre v-if="diagnosticsView.podPreviousLogsSummary" class="diagnostics-snippet">{{ diagnosticsView.podPreviousLogsSummary }}</pre>
        </article>
      </div>

      <details class="diagnostics-raw">
        <summary>Raw diagnostics</summary>
        <pre>{{ diagnosticsView.rawJson }}</pre>
      </details>
    </section>
    </main>
  </div>
</template>
