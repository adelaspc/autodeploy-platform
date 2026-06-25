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
  ["Image pull secret", "image_pull_secret"],
];

function hasValue(value) {
  return value !== undefined && value !== null && value !== "";
}

function displayValue(value) {
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

export function diagnosticStageLabel(stage) {
  return STAGE_LABELS[stage] || stage || "No failure";
}

export function buildDiagnosticsView(diagnostics) {
  const stage = diagnostics?.failure_stage || null;
  const helmFields = fieldList(diagnostics, HELM_FIELDS);
  const resourceFields = fieldList(diagnostics, RESOURCE_FIELDS);
  const missingResources = stringList(diagnostics?.missing_resources);
  const checkedResources = stringList(diagnostics?.checked_resources);
  const podNames = stringList(diagnostics?.pod_names);
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
    missingResources,
    checkedResources,
    podNames,
    hasHelm,
    hasResourceContext:
      resourceFields.length > 0 ||
      missingResources.length > 0 ||
      checkedResources.length > 0 ||
      podNames.length > 0 ||
      hasValue(diagnostics?.pod_describe_summary) ||
      hasValue(diagnostics?.pod_logs_summary),
    helmStdoutSummary: diagnostics?.helm_stdout_summary || "",
    helmStderrSummary: diagnostics?.helm_stderr_summary || "",
    podDescribeSummary: diagnostics?.pod_describe_summary || "",
    podLogsSummary: diagnostics?.pod_logs_summary || "",
    rawJson: diagnostics ? JSON.stringify(diagnostics, null, 2) : "",
  };
}
