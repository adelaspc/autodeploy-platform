<script setup>
import { canCleanupDeployment } from "../serviceReachability.js";

defineProps({ summary: { type: Object, default: null }, liveHealth: { type: Object, default: null }, cleaning: Boolean, statusTone: { type: Function, required: true }, formatTime: { type: Function, required: true } });
defineEmits(["cleanup"]);
</script>

<template>
  <article class="panel">
    <div class="panel-head"><span>summary</span><span v-if="summary" class="subtle">#{{ summary.deployment_id }}</span></div>
    <template v-if="summary">
      <div class="metric-row"><span>Deployment</span><strong :data-tone="statusTone(summary.deployment_status)">{{ summary.deployment_status }}</strong></div>
      <div class="metric-row"><span>Build</span><strong :data-tone="statusTone(summary.build_status)">{{ summary.build_status }}</strong></div>
      <div class="metric-row"><span>Step</span><strong>{{ summary.current_step }}</strong></div>
      <div class="metric-row"><span>Image</span><strong>{{ summary.image_ref || "not built" }}</strong></div>
      <a v-if="summary.service_url && (summary.deploy_target !== 'kubernetes' || summary.kubernetes_ingress_host)" class="service-link" :href="summary.service_url" target="_blank" rel="noopener noreferrer">Open service</a>
      <div v-if="liveHealth" class="live-health" :data-health="liveHealth.status"><span>Live health</span><strong :data-tone="statusTone(liveHealth.status)">{{ liveHealth.status }}</strong><small>{{ liveHealth.message }}</small><small v-if="liveHealth.checked_at">checked {{ formatTime(liveHealth.checked_at) }}</small></div>
      <button v-if="canCleanupDeployment(summary)" class="danger-button" type="button" :disabled="cleaning" @click="$emit('cleanup', summary)">{{ cleaning ? "Cleaning up" : "Cleanup Kubernetes resources" }}</button>
      <div v-if="summary.internal_service_url || (summary.deploy_target === 'kubernetes' && !summary.kubernetes_ingress_host && summary.service_url)" class="metric-row"><span>Internal service</span><strong>{{ summary.internal_service_url || summary.service_url }}</strong></div>
      <p v-if="summary.last_error" class="alert compact">{{ summary.last_error }}</p>
    </template>
    <p v-else class="muted">Select a deployment to inspect it.</p>
  </article>
</template>
