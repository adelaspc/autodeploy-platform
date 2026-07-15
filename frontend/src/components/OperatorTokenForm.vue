<script setup>
const token = defineModel({ type: String, default: "" });
defineEmits(["save", "clear"]);
</script>

<template>
  <form class="token-form" @submit.prevent="$emit('save')">
    <input v-model="token" type="password" placeholder="Bearer token" autocomplete="off" />
    <button type="submit">Save</button>
    <button type="button" @click="$emit('clear')">Clear</button>
  </form>
</template>
