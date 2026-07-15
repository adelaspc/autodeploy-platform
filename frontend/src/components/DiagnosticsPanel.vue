<script setup>
defineProps({
  diagnostics: { type: Object, default: null },
  view: { type: Object, required: true },
  formatTime: { type: Function, required: true },
});
defineEmits(["copy", "download"]);
</script>

<template>
  <section v-if="diagnostics" class="panel diagnostics-panel" id="diagnostics">
    <div class="panel-head">
      <span>kubernetes diagnostics</span>
      <div class="actions-row">
        <button type="button" @click="$emit('copy')">Copy bundle</button>
        <button type="button" @click="$emit('download')">Download bundle</button>
        <span class="subtle">{{ view.stageLabel }}</span>
      </div>
    </div>
    <div class="diagnostics-summary" :data-stage="view.stage || 'none'">
      <span>{{ view.stageLabel }}</span><strong>{{ view.summary }}</strong>
      <small>{{ view.eventType }}<template v-if="view.eventAt"> - {{ formatTime(view.eventAt) }}</template></small>
    </div>
    <div v-if="view.insights.length" class="diagnostics-insights">
      <article v-for="insight in view.insights" :key="insight.title" class="diagnostics-insight">
        <span>Likely cause</span><strong>{{ insight.title }}</strong><p>{{ insight.detail }}</p><small>{{ insight.action }}</small>
      </article>
    </div>
    <div class="diagnostics-grid">
      <article v-if="view.hasHelm" class="diagnostics-section">
        <h2>Helm</h2>
        <div v-if="view.helmFields.length" class="diagnostics-fields">
          <div v-for="field in view.helmFields" :key="field.label" class="metric-row"><span>{{ field.label }}</span><strong>{{ field.value }}</strong></div>
        </div>
        <p v-if="view.helmStderrSummary" class="alert compact">{{ view.helmStderrSummary }}</p>
        <pre v-if="view.helmStdoutSummary" class="diagnostics-snippet">{{ view.helmStdoutSummary }}</pre>
      </article>
      <article v-if="view.hasResourceContext" class="diagnostics-section">
        <h2>Kubernetes</h2>
        <div v-if="view.podFields.length" class="diagnostics-fields">
          <div v-for="field in view.podFields" :key="field.label" class="metric-row"><span>{{ field.label }}</span><strong>{{ field.value }}</strong></div>
        </div>
        <div v-if="view.resourceFields.length" class="diagnostics-fields">
          <div v-for="field in view.resourceFields" :key="field.label" class="metric-row"><span>{{ field.label }}</span><strong>{{ field.value }}</strong></div>
        </div>
        <div v-if="view.missingResources.length" class="diagnostics-list"><span>Missing resources</span><strong>{{ view.missingResources.join(", ") }}</strong></div>
        <div v-if="view.checkedResources.length" class="diagnostics-list"><span>Checked resources</span><strong>{{ view.checkedResources.join(", ") }}</strong></div>
        <div v-if="view.podNames.length" class="diagnostics-list"><span>Pods</span><strong>{{ view.podNames.join(", ") }}</strong></div>
        <pre v-if="view.podDescribeSummary" class="diagnostics-snippet">{{ view.podDescribeSummary }}</pre>
        <pre v-if="view.podLogsSummary" class="diagnostics-snippet">{{ view.podLogsSummary }}</pre>
        <pre v-if="view.podPreviousLogsSummary" class="diagnostics-snippet">{{ view.podPreviousLogsSummary }}</pre>
      </article>
    </div>
    <details class="diagnostics-raw"><summary>Raw diagnostics</summary><pre>{{ view.rawJson }}</pre></details>
  </section>
</template>
