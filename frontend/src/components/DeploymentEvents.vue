<script setup>
defineProps({ events: { type: Array, default: () => [] }, statusTone: { type: Function, required: true }, formatTime: { type: Function, required: true } });
</script>

<template>
  <article class="panel events-panel" id="events">
    <div class="panel-head"><span>events</span><span class="subtle">{{ events.length }}</span></div>
    <div class="event-list">
      <div v-for="event in events" :key="event.id" class="event-row"><span :data-tone="statusTone(event.status)">{{ event.status }}</span><strong>{{ event.event_type }}</strong><small>{{ event.step || "step unknown" }} - {{ formatTime(event.created_at) }}</small><p v-if="event.message">{{ event.message }}</p></div>
      <p v-if="!events.length" class="muted">No events loaded.</p>
    </div>
  </article>
</template>
