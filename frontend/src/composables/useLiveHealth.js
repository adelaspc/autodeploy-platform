import { ref } from "vue";

export function useLiveHealth(request, { intervalMs = 2000, schedule = setInterval, cancel = clearInterval } = {}) {
  const serviceReachability = ref(null);
  let timer = null;
  let requestInFlight = false;

  async function check(projectId, summary) {
    if (!summary || !projectId || requestInFlight) return;
    if (summary.deployment_status !== "running") { serviceReachability.value = null; return; }
    requestInFlight = true;
    if (!serviceReachability.value) serviceReachability.value = { status: "checking", message: "Checking workload health..." };
    try {
      serviceReachability.value = await request(`/api/projects/${projectId}/deployments/${summary.deployment_id}/live-health`);
    } catch (error) {
      serviceReachability.value = { status: "unavailable", message: `Health monitor unavailable: ${error.message}`, checked_at: new Date().toISOString() };
    } finally { requestInFlight = false; }
  }

  function stop() { if (timer !== null) { cancel(timer); timer = null; } }
  function start(projectId, summaryProvider) {
    stop();
    if (summaryProvider()?.deployment_status === "running") timer = schedule(() => check(projectId, summaryProvider()), intervalMs);
  }
  function reset() { stop(); serviceReachability.value = null; }

  return { serviceReachability, check, start, stop, reset };
}
