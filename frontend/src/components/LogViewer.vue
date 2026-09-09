<script setup>
const tailLines = defineModel("tailLines", { type: Number, default: 200 });
defineProps({ buildLog: { type: Object, default: null }, runtimeLog: { type: Object, default: null }, summary: { type: Object, default: null }, enabled: Boolean });
defineEmits(["reload"]);
</script>

<template>
  <section class="log-grid" id="logs">
    <article class="panel log-panel"><div class="panel-head"><span>build log</span><button type="button" :disabled="!enabled" @click="$emit('reload')">Reload</button></div><pre v-if="buildLog?.content">{{ buildLog.content }}</pre><p v-else-if="summary?.build_log_state === 'retention_removed'" class="muted">Build log removed by the retention policy.</p><p v-else class="muted">No build log was produced.</p></article>
    <article class="panel log-panel"><div class="panel-head"><span>runtime log</span><label class="tail-control"><span>tail</span><input v-model.number="tailLines" type="number" min="1" max="2000" @change="$emit('reload')" /></label></div><pre v-if="runtimeLog?.content">{{ runtimeLog.content }}</pre><p v-else-if="summary?.runtime_log_state === 'retention_removed'" class="muted">Runtime log removed by the retention policy.</p><p v-else class="muted">No runtime log was produced.</p></article>
  </section>
</template>
