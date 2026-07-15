import { computed, ref } from "vue";

export function useProjects(request) {
  const projects = ref([]);
  const selectedProjectId = ref(null);
  const selectedProject = computed(() => projects.value.find((project) => project.id === selectedProjectId.value) || null);

  async function loadProjects() {
    projects.value = await request("/api/projects");
    if (selectedProjectId.value && !projects.value.some((project) => project.id === selectedProjectId.value)) selectedProjectId.value = null;
    return projects.value;
  }

  async function persistProject(payload, mode) {
    return mode === "edit" && selectedProjectId.value
      ? request(`/api/projects/${selectedProjectId.value}`, { method: "PATCH", body: JSON.stringify(payload) })
      : request("/api/projects", { method: "POST", body: JSON.stringify(payload) });
  }

  async function removeSelectedProject() {
    if (!selectedProjectId.value) return;
    await request(`/api/projects/${selectedProjectId.value}`, { method: "DELETE" });
    selectedProjectId.value = null;
  }

  return { projects, selectedProjectId, selectedProject, loadProjects, persistProject, removeSelectedProject };
}
