<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { apiRequest, storeToken, storedToken } from "./api";
import { buildDiagnosticsView } from "./diagnostics";
import { diagnosticsBundleFilename, diagnosticsBundleText } from "./diagnosticsBundle";
import { blankProjectForm, projectPayload, projectToForm } from "./projectForm";
import { formatTime, shortSha, statusTone } from "./presentation";
import { useDeployments } from "./composables/useDeployments";
import { useLiveHealth } from "./composables/useLiveHealth";
import { useProjects } from "./composables/useProjects";
import ObservabilityPanel from "./components/ObservabilityPanel.vue";
import OperatorTokenForm from "./components/OperatorTokenForm.vue";
import PlatformStatusGrid from "./components/PlatformStatusGrid.vue";
import DiagnosticsPanel from "./components/DiagnosticsPanel.vue";
import DeploymentActions from "./components/DeploymentActions.vue";
import DeploymentEvents from "./components/DeploymentEvents.vue";
import DeploymentHistory from "./components/DeploymentHistory.vue";
import DeploymentSummary from "./components/DeploymentSummary.vue";
import LogViewer from "./components/LogViewer.vue";
import ProjectForm from "./components/ProjectForm.vue";
import ProjectPicker from "./components/ProjectPicker.vue";

const { projects, selectedProjectId, selectedProject, loadProjects, persistProject, removeSelectedProject } = useProjects(apiRequest);
const {
  projectStatus, projectActivity, selectedDeployment, deploymentSummary, deploymentEvents, buildLog, runtimeLog,
  diagnostics, logTailLines, latestDeployments, activeDeployment, latestFailedDeployment, latestDeployment,
  clearSelection, clearProject, loadProject, selectDeployment: selectDeploymentData, loadArtifacts,
} = useDeployments(apiRequest);
const liveHealth = useLiveHealth(apiRequest);
const serviceReachability = liveHealth.serviceReachability;
const platformHealth = ref(null);
const platformActivity = ref(null);
const observabilityHealth = ref(null);
const apiToken = ref(storedToken());
const globalError = ref("");
const globalMessage = ref("");
const isLoading = ref(false);
const isDeploying = ref(false);
const isSavingProject = ref(false);
const isDeletingProject = ref(false);
const isCleaningDeployment = ref(false);
const formMode = ref("create");
const projectForm = ref(blankProjectForm());
const branchOverride = ref("");
const testCommandOverride = ref("");
const diagnosticsView = computed(() => buildDiagnosticsView(diagnostics.value));
const deploymentCreationReady = computed(() => platformHealth.value?.deployment_creation_ready !== false);

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

async function selectProject(projectId) {
  liveHealth.reset();
  selectedProjectId.value = projectId;
  clearSelection();
  branchOverride.value = selectedProject.value?.branch || "";
  testCommandOverride.value = selectedProject.value?.default_test_command || "";
  editSelectedProject();
  await refreshProject();
}

async function refreshProject() {
  if (!selectedProjectId.value) {
    clearProject();
    return;
  }
  globalError.value = "";
  try {
    await loadProject(selectedProjectId.value);
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

async function saveProject() {
  isSavingProject.value = true;
  try {
    const savedMode = formMode.value;
    const saved = await persistProject(projectPayload(projectForm.value), savedMode);
    await loadProjects();
    await selectProject(saved.id);
    setNotice(savedMode === "edit" ? "Project saved" : "Project created");
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
    const deletedName = selectedProject.value.name;
    await removeSelectedProject();
    setNotice(`Deleted project ${deletedName}`);
    clearProject();
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
      "Kubernetes cleanup requested",
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
  liveHealth.reset();
  try {
    const summary = await selectDeploymentData(selectedProjectId.value, deployment);
    if (summary.service_url && (summary.deploy_target !== "kubernetes" || summary.kubernetes_ingress_host)) {
      await liveHealth.check(selectedProjectId.value, summary);
      liveHealth.start(selectedProjectId.value, () => deploymentSummary.value);
    }
  } catch (error) {
    setError(error);
  }
}

async function loadLogsAndDiagnostics(summary = deploymentSummary.value) {
  try {
    await loadArtifacts(selectedProjectId.value, summary);
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

function saveToken() {
  storeToken(apiToken.value);
  initialize();
}

function clearToken() {
  apiToken.value = "";
  storeToken("");
  initialize();
}

onMounted(initialize);
onBeforeUnmount(liveHealth.stop);
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
        <OperatorTokenForm v-model="apiToken" @save="saveToken" @clear="clearToken" />
      </section>

      <section class="hero-strip" id="overview">
        <div>
          <p class="eyebrow">operator console</p>
          <h1>AutoDeploy Control Plane</h1>
        </div>
        <button type="button" @click="initialize">{{ isLoading ? "Refreshing" : "Refresh" }}</button>
      </section>

      <p v-if="globalError" class="alert">Error: {{ globalError }}</p>
      <p v-if="globalMessage" class="notice">{{ globalMessage }}</p>

      <PlatformStatusGrid
        :platform-health="platformHealth"
        :platform-activity="platformActivity"
        :deployment-creation-ready="deploymentCreationReady"
        :status-tone="statusTone"
      />

      <ObservabilityPanel :observability-health="observabilityHealth" :platform-activity="platformActivity" />

      <section class="dashboard-grid">
        <ProjectPicker :projects="projects" :selected-project-id="selectedProjectId" @select="selectProject" @create="newProject" />
        <DeploymentActions
          v-model:branch="branchOverride" v-model:test-command="testCommandOverride" :project="selectedProject"
          :active-deployment="activeDeployment" :latest-deployment="latestDeployment" :latest-failed-deployment="latestFailedDeployment"
          :is-deploying="isDeploying" :deployment-creation-ready="deploymentCreationReady"
          @deploy="triggerDeploy" @redeploy="redeployLatest" @retry="retryDeployment" @stop="stopDeployment"
        />
      </section>

      <ProjectForm
        v-model="projectForm" :mode="formMode" :selected-project="selectedProject"
        :saving="isSavingProject" :deleting="isDeletingProject"
        @submit="saveProject" @reset="newProject" @delete="deleteSelectedProject" @use-selected="editSelectedProject"
      />
      <DeploymentHistory
        :deployments="latestDeployments" :selected-deployment-id="selectedDeployment?.deployment_id"
        :has-project="Boolean(selectedProjectId)" :status-tone="statusTone" :format-time="formatTime" :short-sha="shortSha"
        @select="selectDeployment" @refresh="refreshProject"
      />
    <section class="details-grid">
      <DeploymentSummary
        :summary="deploymentSummary" :live-health="serviceReachability" :cleaning="isCleaningDeployment"
        :status-tone="statusTone" :format-time="formatTime" @cleanup="cleanupDeployment"
      />
      <DeploymentEvents :events="deploymentEvents" :status-tone="statusTone" :format-time="formatTime" />
    </section>
    <LogViewer
      v-model:tail-lines="logTailLines" :build-log="buildLog" :runtime-log="runtimeLog"
      :enabled="Boolean(deploymentSummary)" @reload="loadLogsAndDiagnostics"
    />
    <DiagnosticsPanel
      :diagnostics="diagnostics"
      :view="diagnosticsView"
      :format-time="formatTime"
      @copy="copyDiagnosticsBundle"
      @download="downloadDiagnosticsBundle"
    />
    </main>
  </div>
</template>
