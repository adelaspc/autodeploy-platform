import assert from "node:assert/strict";
import test from "node:test";

import { useDeployments } from "./composables/useDeployments.js";
import { useProjects } from "./composables/useProjects.js";


test("operator workflow loads a project and selects its Kubernetes deployment", async () => {
  const calls = [];
  const request = async (path) => {
    calls.push(path);
    if (path === "/api/projects") return [{ id: 3, name: "demo", branch: "main" }];
    if (path === "/api/projects/3/status") return { latest_deployment: { deployment_id: 8 } };
    if (path === "/api/projects/3/activity?latest_limit=10&webhook_limit=5") {
      return { latest_deployments: [{ deployment_id: 8, status: "failed" }] };
    }
    if (path === "/api/projects/3/deployments/8/summary") {
      return { deployment_id: 8, deployment_status: "failed", build_status: "succeeded", deploy_target: "kubernetes" };
    }
    if (path === "/api/projects/3/deployments/8/events") return [{ id: 1, event_type: "deployment.failed" }];
    if (path.includes("/build-log")) return { content: "build completed" };
    if (path.includes("/runtime-log")) return { content: "pod failed" };
    if (path.endsWith("/kubernetes-diagnostics")) return { summary: "CrashLoopBackOff" };
    throw new Error(`unexpected request: ${path}`);
  };

  const projects = useProjects(request);
  const deployments = useDeployments(request);
  await projects.loadProjects();
  projects.selectedProjectId.value = projects.projects.value[0].id;
  await deployments.loadProject(projects.selectedProjectId.value);
  await deployments.selectDeployment(projects.selectedProjectId.value, deployments.latestDeployment.value);

  assert.equal(projects.selectedProject.value.name, "demo");
  assert.equal(deployments.deploymentSummary.value.deployment_id, 8);
  assert.equal(deployments.deploymentEvents.value[0].event_type, "deployment.failed");
  assert.equal(deployments.buildLog.value.content, "build completed");
  assert.equal(deployments.runtimeLog.value.content, "pod failed");
  assert.equal(deployments.diagnostics.value.summary, "CrashLoopBackOff");
  assert.ok(calls.includes("/api/projects/3/deployments/8/kubernetes-diagnostics"));
});
