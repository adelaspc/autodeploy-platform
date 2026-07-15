import { computed, ref } from "vue";

export function useDeployments(request) {
  const projectStatus = ref(null);
  const projectActivity = ref(null);
  const selectedDeployment = ref(null);
  const deploymentSummary = ref(null);
  const deploymentEvents = ref([]);
  const buildLog = ref(null);
  const runtimeLog = ref(null);
  const diagnostics = ref(null);
  const logTailLines = ref(200);
  const latestDeployments = computed(() => projectActivity.value?.latest_deployments || []);
  const activeDeployment = computed(() => projectStatus.value?.active_deployment || null);
  const latestFailedDeployment = computed(() => projectStatus.value?.latest_failed_deployment || null);
  const latestDeployment = computed(() => projectStatus.value?.latest_deployment || null);

  function clearSelection() {
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
    [buildLog.value, runtimeLog.value, diagnostics.value] = await Promise.all([
      optionalRequest(`${base}/build-log?tail_lines=${logTailLines.value}`),
      optionalRequest(`${base}/runtime-log?tail_lines=${logTailLines.value}`),
      summary.deploy_target === "kubernetes" ? optionalRequest(`${base}/kubernetes-diagnostics`) : null,
    ]);
  }

  async function selectDeployment(projectId, deployment) {
    if (!deployment?.deployment_id || !projectId) return null;
    clearSelection(); selectedDeployment.value = deployment;
    const base = `/api/projects/${projectId}/deployments/${deployment.deployment_id}`;
    const [summary, events] = await Promise.all([request(`${base}/summary`), request(`${base}/events`)]);
    deploymentSummary.value = summary; deploymentEvents.value = events;
    selectedDeployment.value = { ...deployment, deployment_id: summary.deployment_id, status: summary.deployment_status, build_status: summary.build_status, deploy_target: summary.deploy_target, service_url: summary.service_url, commit_sha: summary.commit_sha, image_ref: summary.image_ref, created_at: summary.created_at, updated_at: summary.updated_at };
    await loadArtifacts(projectId, summary);
    return summary;
  }

  return {
    projectStatus, projectActivity, selectedDeployment, deploymentSummary, deploymentEvents, buildLog, runtimeLog,
    diagnostics, logTailLines, latestDeployments, activeDeployment, latestFailedDeployment, latestDeployment,
    clearSelection, clearProject, loadProject, selectDeployment, loadArtifacts,
  };
}
