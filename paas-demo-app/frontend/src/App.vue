<script setup>
import { computed, onMounted, ref } from "vue";
import { apiRequest, storeToken, storedToken } from "./api";

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
const formMode = ref("create");
const projectForm = ref(blankProjectForm());
const branchOverride = ref("");
const testCommandOverride = ref("");
const logTailLines = ref(200);

const selectedProject = computed(() =>
  projects.value.find((project) => project.id === selectedProjectId.value) || null,
);
const latestDeployments = computed(() => projectActivity.value?.latest_deployments || []);
const activeDeployment = computed(() => projectStatus.value?.active_deployment || null);
const latestFailedDeployment = computed(() => projectStatus.value?.latest_failed_deployment || null);
const latestDeployment = computed(() => projectStatus.value?.latest_deployment || null);
const showKubernetesDiagnosticsPendingMessage = computed(() => {
  if (!deploymentSummary.value || diagnostics.value) {
    return false;
  }
  if (deploymentSummary.value.deploy_target === "kubernetes") {
    return true;
  }
  return (
    platformHealth.value?.executor === "kubernetes" &&
    ["pending", "cloning", "building", "testing", "pushing_image", "deploying"].includes(
      deploymentSummary.value.deployment_status,
    )
  );
});
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
  const [health, activity] = await Promise.all([
    apiRequest("/health/platform"),
    apiRequest("/health/activity?latest_limit=8&active_limit=8&failed_limit=8"),
  ]);
  platformHealth.value = health;
  platformActivity.value = activity;
}

async function loadProjects() {
  projects.value = await apiRequest("/api/projects");
  if (selectedProjectId.value && !projects.value.some((project) => project.id === selectedProjectId.value)) {
    selectedProjectId.value = null;
  }
}

async function selectProject(projectId) {
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
    if (!selectedDeployment.value && latestDeployment.value) {
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
  try {
    const deploymentId = deployment.deployment_id;
    const summary = await apiRequest(`/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/summary`);
    const events = await apiRequest(`/api/projects/${selectedProjectId.value}/deployments/${deploymentId}/events`);
    deploymentSummary.value = summary;
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
  if (["ok", "running", "succeeded", "reachable"].includes(status)) {
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

onMounted(initialize);
</script>

<template>
  <main class="screen">
    <section class="topbar">
      <div>
        <p class="eyebrow">operator console</p>
        <h1>PaaS Control Plane</h1>
      </div>
      <form class="token-form" @submit.prevent="saveToken">
        <input v-model="apiToken" type="password" placeholder="Bearer token" autocomplete="off" />
        <button type="submit">Save</button>
        <button type="button" @click="clearToken">Clear</button>
      </form>
    </section>

    <p v-if="globalError" class="alert">Error: {{ globalError }}</p>
    <p v-if="globalMessage" class="notice">{{ globalMessage }}</p>

    <section class="status-grid">
      <article class="panel">
        <div class="panel-head">
          <span>platform</span>
          <button type="button" @click="initialize">{{ isLoading ? "Refreshing" : "Refresh" }}</button>
        </div>
        <div class="metric-row">
          <span>API</span>
          <strong :data-tone="statusTone(platformHealth?.status)">{{ platformHealth?.status || "unknown" }}</strong>
        </div>
        <div class="metric-row">
          <span>Executor</span>
          <strong>{{ platformHealth?.executor || "unknown" }}</strong>
        </div>
        <div class="metric-row">
          <span>Deploy ready</span>
          <strong :data-tone="deploymentCreationReady ? 'success' : 'danger'">
            {{ deploymentCreationReady ? "yes" : "no" }}
          </strong>
        </div>
        <p v-if="platformHealth?.deployment_creation_error" class="muted">
          {{ platformHealth.deployment_creation_error }}
        </p>
      </article>

      <article class="panel">
        <div class="panel-head">
          <span>activity</span>
          <span class="subtle">{{ platformActivity?.latest_deployment_at ? "live" : "idle" }}</span>
        </div>
        <div class="metric-row">
          <span>Active</span>
          <strong>{{ platformActivity?.active_deployment_count ?? 0 }}</strong>
        </div>
        <div class="metric-row">
          <span>Failed</span>
          <strong>{{ platformActivity?.failed_deployment_count ?? 0 }}</strong>
        </div>
        <div class="metric-row">
          <span>Webhooks</span>
          <strong>{{ platformActivity?.recent_webhook_delivery_count ?? 0 }}</strong>
        </div>
      </article>

      <article class="panel project-picker">
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
          <small>{{ project.branch }} - {{ project.runtime }}</small>
        </button>
        <p v-if="!projects.length" class="muted">No projects returned by the API.</p>
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
      <article class="panel deploy-panel">
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
          <a v-if="deploymentSummary.service_url" class="service-link" :href="deploymentSummary.service_url" target="_blank">
            Open service
          </a>
          <p v-if="deploymentSummary.last_error" class="alert compact">{{ deploymentSummary.last_error }}</p>
        </template>
        <p v-else class="muted">Select a deployment to inspect it.</p>
      </article>

      <article class="panel events-panel">
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

    <section class="log-grid">
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

    <section v-if="diagnostics" class="panel diagnostics-panel">
      <div class="panel-head">
        <span>kubernetes diagnostics</span>
        <span class="subtle">{{ diagnostics.failure_stage || "no failure" }}</span>
      </div>
      <pre>{{ JSON.stringify(diagnostics, null, 2) }}</pre>
    </section>
    <section v-else-if="showKubernetesDiagnosticsPendingMessage" class="panel diagnostics-panel">
      <div class="panel-head">
        <span>kubernetes diagnostics</span>
        <span class="subtle">pending</span>
      </div>
      <p class="muted diagnostics-placeholder">Kubernetes diagnostics will appear after the deploy phase starts.</p>
    </section>
  </main>
</template>
