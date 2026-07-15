export function canCleanupDeployment(deployment) {
  const status = deployment?.deployment_status || deployment?.status;
  return deployment?.deploy_target === "kubernetes" && ["running", "failed", "stopped"].includes(status);
}
