<script setup>
const branch = defineModel("branch", { type: String, default: "" });
const testCommand = defineModel("testCommand", { type: String, default: "" });
defineProps({
  project: { type: Object, default: null }, activeDeployment: { type: Object, default: null },
  latestDeployment: { type: Object, default: null }, latestFailedDeployment: { type: Object, default: null },
  isDeploying: { type: Boolean, default: false }, deploymentCreationReady: { type: Boolean, default: true },
});
defineEmits(["deploy", "redeploy", "retry", "stop"]);
</script>

<template>
  <article class="panel deploy-panel" id="deployments">
    <div class="panel-head"><span>run deploy / test</span><span v-if="project" class="subtle">project #{{ project.id }}</span></div>
    <template v-if="project">
      <div class="project-summary"><h2>{{ project.name }}</h2><p>{{ project.repo_url }}</p></div>
      <form class="deploy-form" @submit.prevent="$emit('deploy')">
        <label><span>Branch</span><input v-model.trim="branch" placeholder="main" /></label>
        <label><span>Test command</span><input v-model="testCommand" placeholder="pytest, npm test, or leave empty" /></label>
        <button type="submit" :disabled="isDeploying || !deploymentCreationReady">{{ isDeploying ? "Queuing" : "Run deploy / test" }}</button>
      </form>
      <div class="actions-row">
        <button type="button" :disabled="!latestDeployment" @click="$emit('redeploy')">Redeploy latest</button>
        <button type="button" :disabled="latestFailedDeployment?.status !== 'failed'" @click="$emit('retry', latestFailedDeployment)">Retry failed</button>
        <button type="button" :disabled="activeDeployment?.status !== 'running'" @click="$emit('stop', activeDeployment)">Stop running</button>
      </div>
    </template>
    <p v-else class="muted">Select a project to run deployments.</p>
  </article>
</template>
