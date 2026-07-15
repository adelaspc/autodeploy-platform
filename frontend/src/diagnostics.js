const STAGE_LABELS = {
  helm: "Helm",
  preflight: "Preflight",
  manifest_apply: "Manifest apply",
  rollout: "Rollout",
  healthcheck: "Healthcheck",
};

const HELM_FIELDS = [
  ["Release", "helm_release_name"],
  ["Namespace", "helm_namespace"],
  ["Chart", "helm_chart_path"],
  ["Release status", "helm_release_status"],
  ["Return code", "helm_returncode"],
  ["Log path", "helm_log_path"],
];

const RESOURCE_FIELDS = [
  ["Namespace", "namespace"],
  ["Deployment", "deployment_name"],
  ["Service", "service_name"],
  ["Ingress", "ingress_name"],
  ["Ingress host", "ingress_host"],
  ["Ingress class", "ingress_class"],
  ["Internal service URL", "internal_service_url"],
  ["Image pull secret", "image_pull_secret"],
];

const POD_FIELDS = [
  ["Pod phase", "pod_phase"],
  ["Container reason", "container_reason"],
  ["Restart count", "restart_count"],
  ["Images", "images"],
  ["Image pull secrets", "image_pull_secrets"],
];

function hasValue(value) {
  return value !== undefined && value !== null && value !== "";
}

function displayValue(value) {
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "none";
  }
  return hasValue(value) ? String(value) : "not recorded";
}

function fieldList(source, fields) {
  return fields
    .map(([label, key]) => ({ label, value: displayValue(source?.[key]) }))
    .filter((field) => field.value !== "not recorded");
}

function resourceLabel(resource) {
  if (typeof resource === "string") {
    return resource;
  }
  if (!resource || typeof resource !== "object") {
    return null;
  }
  const kind = resource.kind || resource.resource_kind;
  const name = resource.name || resource.resource_name;
  if (kind && name) {
    return `${kind}/${name}`;
  }
  return name || kind || null;
}

function stringList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(resourceLabel).filter(Boolean);
}

function combinedDiagnosticText(diagnostics) {
  return [
    diagnostics?.failure_summary,
    diagnostics?.pod_describe_summary,
    diagnostics?.pod_logs_summary,
    diagnostics?.pod_previous_logs_summary,
    diagnostics?.rollout_pods_summary,
    diagnostics?.rollout_describe_summary,
    diagnostics?.healthcheck_pods_summary,
    diagnostics?.healthcheck_deployment_summary,
  ]
    .filter(Boolean)
    .join("\n");
}

function diagnosticInsights(diagnostics) {
  const text = combinedDiagnosticText(diagnostics);
  const insights = [];
  if (/\b(ErrImagePull|ImagePullBackOff)\b/i.test(text)) {
    insights.push({
      title: "Image pull is failing",
      detail:
        "Kubernetes created the pod, but kubelet cannot pull the image. Verify the image reference, Docker Hub repository, namespace, and imagePullSecret.",
      action:
        "Set CONTROL_PLANE_K8S_IMAGE_PULL_SECRET to an existing dockerconfigjson secret, recreate the runtime, then start a new deployment.",
    });
  }
  if (/\bCrashLoopBackOff\b/i.test(text) || /Back-off restarting failed container/i.test(text)) {
    insights.push({
      title: "Container is restarting",
      detail:
        "The image was pulled and the container started, but the app process exited. Check previous container logs for missing env vars, bad startup commands, or wrong runtime config.",
      action:
        "Use the Previous logs section first. Confirm the project env vars, port, and healthcheck path match the application.",
    });
  }
  if (/failed to verify certificate|certificate is valid for/i.test(text)) {
    insights.push({
      title: "Kubeconfig TLS mismatch",
      detail:
        "The worker can reach the Kubernetes API endpoint, but TLS verification does not match the hostname used from the container.",
      action: "Regenerate the Compose kubeconfig with make compose-recreate-runtime PROFILE=local-kubernetes.",
    });
  }
  return insights;
}

export function diagnosticStageLabel(stage) {
  return STAGE_LABELS[stage] || stage || "No failure";
}

export function buildDiagnosticsView(diagnostics) {
  const stage = diagnostics?.failure_stage || null;
  const helmFields = fieldList(diagnostics, HELM_FIELDS);
  const resourceFields = fieldList(diagnostics, RESOURCE_FIELDS);
  const podFields = fieldList(diagnostics, POD_FIELDS);
  const missingResources = stringList(diagnostics?.missing_resources);
  const checkedResources = stringList(diagnostics?.checked_resources);
  const podNames = stringList(diagnostics?.pod_names);
  const insights = diagnosticInsights(diagnostics);
  const hasHelm =
    stage === "helm" ||
    [
      "helm_release_name",
      "helm_namespace",
      "helm_chart_path",
      "helm_returncode",
      "helm_stdout_summary",
      "helm_stderr_summary",
      "helm_log_path",
      "helm_release_status",
    ].some((key) => hasValue(diagnostics?.[key]));

  return {
    stage,
    stageLabel: diagnosticStageLabel(stage),
    summary: diagnostics?.failure_summary || "No failure summary recorded.",
    eventType: diagnostics?.failure_event_type || "not recorded",
    eventAt: diagnostics?.failure_event_at || null,
    helmFields,
    resourceFields,
    podFields,
    missingResources,
    checkedResources,
    podNames,
    hasHelm,
    hasResourceContext:
      resourceFields.length > 0 ||
      podFields.length > 0 ||
      missingResources.length > 0 ||
      checkedResources.length > 0 ||
      podNames.length > 0 ||
      hasValue(diagnostics?.pod_describe_summary) ||
      hasValue(diagnostics?.pod_logs_summary) ||
      hasValue(diagnostics?.pod_previous_logs_summary) ||
      insights.length > 0,
    insights,
    helmStdoutSummary: diagnostics?.helm_stdout_summary || "",
    helmStderrSummary: diagnostics?.helm_stderr_summary || "",
    podDescribeSummary: diagnostics?.pod_describe_summary || "",
    podLogsSummary: diagnostics?.pod_logs_summary || "",
    podPreviousLogsSummary: diagnostics?.pod_previous_logs_summary || "",
    rawJson: diagnostics ? JSON.stringify(diagnostics, null, 2) : "",
  };
}
