<script setup>
import { DIAGNOSTICS_BUNDLE_NOTICE } from "../diagnosticsBundle";

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
    <p class="diagnostics-export-notice" role="note">
      <strong>{{ DIAGNOSTICS_BUNDLE_NOTICE }}</strong>
    </p>
    <div class="diagnostics-summary" :data-stage="view.stage || 'none'">
      <span>{{ view.stageLabel }}</span><strong>{{ view.summary }}</strong>
      <small>{{ view.eventType }}<template v-if="view.eventAt"> - {{ formatTime(view.eventAt) }}</template></small>
    </div>
    <div class="diagnostics-summary">
      <strong>Persisted diagnostic snapshot</strong>
      <small v-if="view.snapshotAt">Captured {{ formatTime(view.snapshotAt) }}. Refresh reloads this evidence; it does not query the cluster.</small>
      <small v-else>Capture time was not recorded. Refresh reloads persisted evidence; it does not query the cluster.</small>
    </div>
    <div v-if="view.collectionStatus" class="diagnostics-summary">
      <strong>Collection status: {{ view.collectionStatus }}</strong>
      <small v-if="view.collectedAt">Detailed collection started {{ formatTime(view.collectedAt) }}.</small>
      <p v-if="view.collectionStatus !== 'complete'">Some evidence could not be collected. The original Helm error is preserved.</p>
      <ul v-if="view.collectionErrors?.length">
        <li v-for="(error, index) in view.collectionErrors" :key="index">{{ error.operation }}: {{ error.reason }}</li>
      </ul>
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
        <template v-if="view.deploymentDescribeSummary"><h3>Deployment description</h3><pre class="diagnostics-snippet">{{ view.deploymentDescribeSummary }}</pre></template>
        <template v-if="view.serviceDescribeSummary"><h3>Service description</h3><pre class="diagnostics-snippet">{{ view.serviceDescribeSummary }}</pre></template>
        <template v-if="view.podDescribeSummary"><h3>Pod descriptions and events</h3><pre class="diagnostics-snippet">{{ view.podDescribeSummary }}</pre></template>
        <template v-if="view.podLogsSummary"><h3>Current logs</h3><pre class="diagnostics-snippet">{{ view.podLogsSummary }}</pre></template>
        <template v-if="view.podPreviousLogsSummary"><h3>Previous logs</h3><pre class="diagnostics-snippet">{{ view.podPreviousLogsSummary }}</pre></template>
      </article>
    </div>
    <details class="diagnostics-raw"><summary>Raw diagnostics</summary><pre>{{ view.rawJson }}</pre></details>
  </section>
</template>
