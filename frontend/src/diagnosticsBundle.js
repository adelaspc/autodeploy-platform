export function buildDiagnosticsBundle({ summary, diagnostics, events, buildLog, runtimeLog }) {
  return {
    generated_at: new Date().toISOString(),
    deployment: summary || null,
    kubernetes_diagnostics: diagnostics || null,
    events: events || [],
    build_log: buildLog || null,
    runtime_log: runtimeLog || null,
  };
}

export function diagnosticsBundleText(parts) {
  return JSON.stringify(buildDiagnosticsBundle(parts), null, 2);
}

export function diagnosticsBundleFilename(summary) {
  return `paas-deployment-${summary?.deployment_id || "unknown"}-diagnostics.json`;
}
