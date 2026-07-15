<script setup>
defineProps({ projects: { type: Array, default: () => [] }, selectedProjectId: { type: Number, default: null } });
defineEmits(["select", "create"]);
</script>

<template>
  <article class="panel project-picker" id="projects">
    <div class="panel-head"><span>projects</span><button type="button" @click="$emit('create')">New</button></div>
    <button v-for="project in projects" :key="project.id" class="project-button" :data-active="project.id === selectedProjectId" type="button" @click="$emit('select', project.id)">
      <span>{{ project.name }}</span><small>{{ project.branch }} / {{ project.runtime }}</small>
    </button>
    <p v-if="!projects.length" class="muted">No projects returned by the API.</p>
  </article>
</template>
