<script setup>
const tailLines = defineModel("tailLines", { type: Number, default: 200 });
defineProps({ buildLog: { type: Object, default: null }, runtimeLog: { type: Object, default: null }, enabled: Boolean });
defineEmits(["reload"]);
</script>

<template>
  <section class="log-grid" id="logs">
    <article class="panel log-panel"><div class="panel-head"><span>build log</span><button type="button" :disabled="!enabled" @click="$emit('reload')">Reload</button></div><pre v-if="buildLog?.content">{{ buildLog.content }}</pre><p v-else class="muted">No build log available.</p></article>
    <article class="panel log-panel"><div class="panel-head"><span>runtime log</span><label class="tail-control"><span>tail</span><input v-model.number="tailLines" type="number" min="1" max="2000" @change="$emit('reload')" /></label></div><pre v-if="runtimeLog?.content">{{ runtimeLog.content }}</pre><p v-else class="muted">No runtime log available.</p></article>
  </section>
</template>
