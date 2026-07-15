<script setup>
defineProps({ deployments: { type: Array, default: () => [] }, selectedDeploymentId: { type: Number, default: null }, hasProject: Boolean, statusTone: { type: Function, required: true }, formatTime: { type: Function, required: true }, shortSha: { type: Function, required: true } });
defineEmits(["select", "refresh"]);
</script>

<template>
  <section class="workspace"><article class="panel">
    <div class="panel-head"><span>deployment history</span><button type="button" :disabled="!hasProject" @click="$emit('refresh')">Refresh</button></div>
    <div class="deployment-list">
      <button v-for="deployment in deployments" :key="deployment.deployment_id" class="deployment-row" :data-active="deployment.deployment_id === selectedDeploymentId" type="button" @click="$emit('select', deployment)">
        <span>#{{ deployment.deployment_id }}</span><strong :data-tone="statusTone(deployment.status)">{{ deployment.status }}</strong><small>{{ shortSha(deployment.commit_sha) }}</small><small>{{ formatTime(deployment.created_at) }}</small>
      </button>
      <p v-if="hasProject && !deployments.length" class="muted">No deployments for this project yet.</p>
    </div>
  </article></section>
</template>
