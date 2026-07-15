<script setup>
defineProps({
  observabilityHealth: { type: Object, default: null },
  platformActivity: { type: Object, default: null },
});
</script>

<template>
  <section class="observability-panel panel" id="observability">
    <div class="panel-head"><span>observability</span><span class="subtle">lightweight operational signals</span></div>
    <div class="observability-grid">
      <article class="observability-card">
        <span>Metrics endpoint</span>
        <strong :data-tone="observabilityHealth?.metrics?.enabled ? 'success' : 'neutral'">{{ observabilityHealth?.metrics?.enabled ? "enabled" : "disabled" }}</strong>
        <small>{{ observabilityHealth?.metrics?.endpoint || "/metrics" }} · dedicated bearer token</small>
      </article>
      <article class="observability-card">
        <span>Structured logs</span>
        <strong :data-tone="observabilityHealth?.logging?.structured ? 'success' : 'neutral'">{{ observabilityHealth?.logging?.format || "unknown" }}</strong>
        <small>{{ observabilityHealth?.logging?.destination || "stdout" }} · API / worker / reconciler</small>
      </article>
      <article class="observability-card">
        <span>Request correlation</span><strong data-tone="success">enabled</strong>
        <small>{{ observabilityHealth?.request_correlation?.header || "X-Request-ID" }}</small>
      </article>
      <article class="observability-card">
        <span>Operational activity</span>
        <strong :data-tone="(platformActivity?.failed_deployment_count ?? 0) ? 'danger' : 'success'">
          {{ platformActivity?.active_deployment_count ?? 0 }} active / {{ platformActivity?.failed_deployment_count ?? 0 }} failed
        </strong>
        <small>events, audit, logs and diagnostics retained</small>
      </article>
    </div>
  </section>
</template>
