<script setup>
defineProps({
  platformHealth: { type: Object, default: null },
  platformActivity: { type: Object, default: null },
  deploymentCreationReady: { type: Boolean, required: true },
  statusTone: { type: Function, required: true },
});
</script>

<template>
  <section class="status-grid">
    <article class="panel stat-card">
      <div class="panel-head"><span>api health</span><span class="panel-glyph">API</span></div>
      <strong class="stat-value" :data-tone="statusTone(platformHealth?.status)">{{ platformHealth?.status || "unknown" }}</strong>
      <small>{{ deploymentCreationReady ? "deployment creation ready" : "deployment creation blocked" }}</small>
      <p v-if="platformHealth?.deployment_creation_error" class="muted">{{ platformHealth.deployment_creation_error }}</p>
    </article>
    <article class="panel stat-card">
      <div class="panel-head"><span>active deployments</span><span class="panel-glyph">RUN</span></div>
      <strong class="stat-value" data-tone="warning">{{ platformActivity?.active_deployment_count ?? 0 }}</strong>
      <small>{{ platformActivity?.latest_deployment_at ? "latest activity recorded" : "idle" }}</small>
    </article>
    <article class="panel stat-card">
      <div class="panel-head"><span>failed deployments</span><span class="panel-glyph">ERR</span></div>
      <strong class="stat-value" :data-tone="(platformActivity?.failed_deployment_count ?? 0) ? 'danger' : 'success'">
        {{ platformActivity?.failed_deployment_count ?? 0 }}
      </strong>
      <small>{{ platformActivity?.recent_webhook_delivery_count ?? 0 }} recent webhooks</small>
    </article>
    <article class="panel stat-card">
      <div class="panel-head"><span>executor</span><span class="panel-glyph">CPU</span></div>
      <strong class="stat-value compact-value">{{ platformHealth?.executor || "unknown" }}</strong>
      <small>{{ deploymentCreationReady ? "ready" : "not ready" }}</small>
    </article>
  </section>
</template>
