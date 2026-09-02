import { computed, ref } from "vue";

const ACTIVE_DEPLOYMENT_STATUSES = new Set(["pending", "cloning", "building", "testing", "pushing_image", "deploying"]);

export function useDeployments(
  request,
  { pollIntervalMs = 2000, schedule = setInterval, cancel = clearInterval } = {},
) {
  // Deployment progress is persisted by the backend, so short polling is enough
  // to rebuild the current view after a refresh or temporary network failure.
  const projectStatus = ref(null);
  const projectActivity = ref(null);
  const selectedDeployment = ref(null);
  const deploymentSummary = ref(null);
  const deploymentEvents = ref([]);
  const buildLog = ref(null);
  const runtimeLog = ref(null);
  const diagnostics = ref(null);
  const pollingError = ref(null);
  const logTailLines = ref(200);
  let pollingTimer = null;
  let pollingRequestInFlight = false;
  const latestDeployments = computed(() => projectActivity.value?.latest_deployments || []);
  const activeDeployment = computed(() => projectStatus.value?.active_deployment || null);
  const latestFailedDeployment = computed(() => projectStatus.value?.latest_failed_deployment || null);
  const latestDeployment = computed(() => projectStatus.value?.latest_deployment || null);

  function clearSelection() {
    stopDeploymentPolling();
    selectedDeployment.value = null; deploymentSummary.value = null; deploymentEvents.value = [];
    buildLog.value = null; runtimeLog.value = null; diagnostics.value = null;
  }

  function clearProject() { projectStatus.value = null; projectActivity.value = null; clearSelection(); }

  async function loadProject(projectId) {
    if (!projectId) { clearProject(); return; }
    [projectStatus.value, projectActivity.value] = await Promise.all([
      request(`/api/projects/${projectId}/status`),
      request(`/api/projects/${projectId}/activity?latest_limit=10&webhook_limit=5`),
    ]);
  }

  async function optionalRequest(path) {
    try { return await request(path); } catch (error) { if (error.status === 404) return null; throw error; }
  }

  async function loadArtifacts(projectId, summary = deploymentSummary.value) {
    if (!projectId || !summary) return;
    const base = `/api/projects/${projectId}/deployments/${summary.deployment_id}`;
    // Logs and diagnostics are independent reads; load them together to keep
    // deployment selection responsive.
    [buildLog.value, runtimeLog.value, diagnostics.value] = await Promise.all([
      optionalRequest(`${base}/build-log?tail_lines=${logTailLines.value}`),
      optionalRequest(`${base}/runtime-log?tail_lines=${logTailLines.value}`),
      summary.deploy_target === "kubernetes" ? optionalRequest(`${base}/kubernetes-diagnostics`) : null,
    ]);
  }

  async function selectDeployment(projectId, deployment) {
    if (!deployment?.deployment_id || !projectId) return null;
    clearSelection(); selectedDeployment.value = deployment;
    const summary = await refreshSelectedDeployment(projectId, deployment.deployment_id);
    if (!summary) return null;
    await loadArtifacts(projectId, summary);
    startDeploymentPolling(projectId, summary.deployment_id);
    return summary;
  }

  function updateSelectedDeployment(summary, events) {
    deploymentSummary.value = summary;
    deploymentEvents.value = events;
    selectedDeployment.value = {
      ...selectedDeployment.value,
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
  }

  async function refreshSelectedDeployment(projectId, deploymentId) {
    if (!projectId || !deploymentId) return null;
    const base = `/api/projects/${projectId}/deployments/${deploymentId}`;
    const [summary, events] = await Promise.all([request(`${base}/summary`), request(`${base}/events`)]);
    if (selectedDeployment.value?.deployment_id !== deploymentId) return null;
    updateSelectedDeployment(summary, events);
    pollingError.value = null;
    return summary;
  }

  function stopDeploymentPolling() {
    if (pollingTimer !== null) {
      cancel(pollingTimer);
      pollingTimer = null;
    }
  }

  async function pollSelectedDeployment(projectId, deploymentId) {
    if (pollingRequestInFlight || selectedDeployment.value?.deployment_id !== deploymentId) return;
    pollingRequestInFlight = true;
    try {
      const summary = await refreshSelectedDeployment(projectId, deploymentId);
      if (summary && !ACTIVE_DEPLOYMENT_STATUSES.has(summary.deployment_status)) {
        stopDeploymentPolling();
        await loadArtifacts(projectId, summary);
      }
    } catch (error) {
      pollingError.value = error;
    } finally {
      pollingRequestInFlight = false;
    }
  }

  function startDeploymentPolling(projectId, deploymentId) {
    stopDeploymentPolling();
    if (!ACTIVE_DEPLOYMENT_STATUSES.has(deploymentSummary.value?.deployment_status)) return;
    pollingTimer = schedule(
      () => pollSelectedDeployment(projectId, deploymentId),
      pollIntervalMs,
    );
  }

  return {
    projectStatus, projectActivity, selectedDeployment, deploymentSummary, deploymentEvents, buildLog, runtimeLog,
    diagnostics, pollingError, logTailLines, latestDeployments, activeDeployment, latestFailedDeployment, latestDeployment,
    clearSelection, clearProject, loadProject, selectDeployment, loadArtifacts, refreshSelectedDeployment,
    startDeploymentPolling, stopDeploymentPolling,
  };
}
