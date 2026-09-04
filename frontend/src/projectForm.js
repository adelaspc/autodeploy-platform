export const EMPTY_PROJECT_FORM = Object.freeze({
  name: "",
  repo_url: "",
  branch: "main",
  git_auth_type: "none",
  git_secret_ref: "",
  dockerfile_path: "Dockerfile",
  build_context: ".",
  port: 5000,
  healthcheck_path: "/health",
  default_test_command: "",
  migration_command: "",
  cpu: "",
  memory: "",
  trigger: "manual",
  runtime: "dockerfile",
  env_vars: [],
});

export function blankProjectForm() {
  return structuredClone(EMPTY_PROJECT_FORM);
}

export function blankEnvVar() {
  return { name: "", value_source: "literal", value: "", is_secret: false, source_name: "", source_key: "" };
}

export function syncEnvVarSecretFlag(row) {
  if (row.value_source === "secret_key_ref") row.is_secret = true;
}

export function projectToForm(project) {
  return {
    name: project.name || "",
    repo_url: project.repo_url || "",
    branch: project.branch || "main",
    git_auth_type: project.git_auth_type || "none",
    git_secret_ref: project.git_secret_ref || "",
    dockerfile_path: project.dockerfile_path || "Dockerfile",
    build_context: project.build_context || ".",
    port: project.port || 5000,
    healthcheck_path: project.healthcheck_path || "/health",
    default_test_command: project.default_test_command || "",
    migration_command: project.migration_command || "",
    cpu: project.cpu || "",
    memory: project.memory || "",
    trigger: project.trigger || "manual",
    runtime: project.runtime || "dockerfile",
    env_vars: (project.env_vars || []).map(envVarToFormRow),
  };
}

export function envVarToFormRow(item) {
  return {
    name: item.name || "",
    value_source: item.value_source || "literal",
    value: item.is_secret && item.value === "[REDACTED]" ? "" : item.value || "",
    is_secret: Boolean(item.is_secret),
    source_name: item.source_name || "",
    source_key: item.source_key || "",
  };
}

export function normalizeEnvRowForSubmit(row, index) {
  const name = row.name.trim();
  if (!name) throw new Error(`Env var ${index + 1} needs a name`);
  if (row.value_source === "literal") {
    if (!row.value) throw new Error(`Env var ${name} needs a literal value`);
    return { name, value_source: "literal", value: row.value, is_secret: Boolean(row.is_secret) };
  }
  if (!row.source_name.trim() || !row.source_key.trim()) {
    throw new Error(`Env var ${name} needs source name and source key`);
  }
  return {
    name,
    value_source: row.value_source,
    source_name: row.source_name.trim(),
    source_key: row.source_key.trim(),
    is_secret: row.value_source === "secret_key_ref" ? true : Boolean(row.is_secret),
  };
}

export function projectPayload(form) {
  return {
    name: form.name.trim(),
    repo_url: form.repo_url.trim(),
    branch: form.branch.trim(),
    git_auth_type: form.git_auth_type,
    git_secret_ref: form.git_auth_type === "token" ? form.git_secret_ref.trim() : null,
    dockerfile_path: form.dockerfile_path.trim() || "Dockerfile",
    build_context: form.build_context.trim() || ".",
    port: Number(form.port),
    healthcheck_path: form.healthcheck_path.trim(),
    env_vars: form.env_vars.map(normalizeEnvRowForSubmit),
    default_test_command: form.default_test_command.trim() || null,
    migration_command: form.migration_command.trim() || null,
    cpu: form.cpu.trim() || null,
    memory: form.memory.trim() || null,
    trigger: form.trigger,
    runtime: form.runtime,
  };
}
