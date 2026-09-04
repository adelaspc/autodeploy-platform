<script setup>
import { blankEnvVar, syncEnvVarSecretFlag } from "../projectForm.js";

const form = defineModel({ type: Object, required: true });
defineProps({ mode: { type: String, required: true }, selectedProject: { type: Object, default: null }, saving: Boolean, deleting: Boolean });
defineEmits(["submit", "reset", "delete", "use-selected"]);

function addEnvVar() { form.value.env_vars.push(blankEnvVar()); }
function removeEnvVar(index) { form.value.env_vars.splice(index, 1); }
</script>

<template>
  <section class="project-editor">
    <article class="panel">
      <div class="panel-head"><span>{{ mode === "edit" ? "edit project" : "create project" }}</span><button type="button" :disabled="!selectedProject" @click="$emit('use-selected')">Use selected</button></div>
      <form class="project-form" @submit.prevent="$emit('submit')">
        <label><span>Name</span><input v-model.trim="form.name" required /></label>
        <label class="wide"><span>Repository URL</span><input v-model.trim="form.repo_url" required placeholder="https://github.com/user/repo.git" /></label>
        <label><span>Branch</span><input v-model.trim="form.branch" required /></label>
        <label><span>Port</span><input v-model.number="form.port" required type="number" min="1" max="65535" /></label>
        <label><span>Dockerfile</span><input v-model.trim="form.dockerfile_path" required /></label>
        <label><span>Build context</span><input v-model.trim="form.build_context" required /></label>
        <label><span>Healthcheck path</span><input v-model.trim="form.healthcheck_path" required /></label>
        <label><span>Default test command</span><input v-model="form.default_test_command" placeholder="pytest, npm test, or empty" /></label>
        <label><span>Migration command</span><input v-model="form.migration_command" placeholder="optional" /></label>
        <label><span>CPU</span><input v-model.trim="form.cpu" placeholder="250m" /></label>
        <label><span>Memory</span><input v-model.trim="form.memory" placeholder="512Mi" /></label>
        <label><span>Trigger</span><select v-model="form.trigger"><option value="manual">manual</option><option value="github_push">github_push</option></select></label>
        <label><span>Runtime</span><select v-model="form.runtime"><option value="dockerfile">dockerfile</option></select></label>
        <label><span>Git auth</span><select v-model="form.git_auth_type"><option value="none">none</option><option value="token">token</option></select></label>
        <label><span>Git secret ref</span><input v-model.trim="form.git_secret_ref" :disabled="form.git_auth_type !== 'token'" /></label>
        <div class="env-editor wide">
          <div class="panel-head inline-head"><span>environment variables</span><button type="button" @click="addEnvVar">Add env var</button></div>
          <div v-if="form.env_vars.length" class="env-list">
            <div v-for="(envVar, index) in form.env_vars" :key="index" class="env-row">
              <label><span>Name</span><input v-model.trim="envVar.name" /></label>
              <label><span>Source</span><select v-model="envVar.value_source" @change="syncEnvVarSecretFlag(envVar)"><option value="literal">literal</option><option value="configmap_key_ref">configmap key</option><option value="secret_key_ref">secret key</option></select></label>
              <label v-if="envVar.value_source === 'literal'"><span>Value</span><input v-model="envVar.value" :type="envVar.is_secret ? 'password' : 'text'" /></label>
              <label v-else><span>Source name</span><input v-model.trim="envVar.source_name" /></label>
              <label v-if="envVar.value_source !== 'literal'"><span>Source key</span><input v-model.trim="envVar.source_key" /></label>
              <label class="checkbox-label"><input v-model="envVar.is_secret" type="checkbox" :disabled="envVar.value_source === 'secret_key_ref'" /><span>secret</span></label>
              <button type="button" @click="removeEnvVar(index)">Remove</button>
            </div>
          </div>
          <p v-else class="muted">No env vars configured.</p>
        </div>
        <div class="form-actions wide">
          <button type="submit" :disabled="saving">{{ saving ? "Saving" : mode === "edit" ? "Save project" : "Create project" }}</button>
          <button type="button" @click="$emit('reset')">Reset</button>
          <button v-if="mode === 'edit'" class="danger-button" type="button" :disabled="deleting" @click="$emit('delete')">{{ deleting ? "Deleting" : "Delete project" }}</button>
        </div>
      </form>
    </article>
  </section>
</template>
