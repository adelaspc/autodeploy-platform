<script setup>
import { computed, onMounted, ref } from "vue";

const deployments = ref([]);
const serviceHealth = ref("checking");
const webHealth = ref("checking");
const lastHealthCheckAt = ref(null);
const isRefreshingHealth = ref(false);
const isSubmitting = ref(false);
const updatingDeploymentIds = ref({});
const deletingDeploymentIds = ref({});
const errorMessage = ref("");
const statusOptions = ["pending", "building", "deployed", "failed"];
const pagination = ref({
  page: 1,
  per_page: 10,
  total: 0,
  pages: 0,
  has_next: false,
  has_prev: false,
});
const historyFilters = ref({
  application_name: "",
  environment: "",
  status: "",
});

const form = ref({
  application_name: "",
  version: "",
  environment: "staging",
  status: "pending",
});

const sortedDeployments = computed(() => deployments.value);
const serviceStatusTone = computed(() => mapHealthTone(serviceHealth.value));
const serviceStatusLabel = computed(() => formatHealthLabel(serviceHealth.value));
const webStatusTone = computed(() => mapHealthTone(webHealth.value));
const webStatusLabel = computed(() => formatHealthLabel(webHealth.value));

async function readResponsePayload(response) {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

async function fetchHealth() {
  isRefreshingHealth.value = true;

  try {
    const [webStatus, serviceStatus] = await Promise.all([
      probeHealthEndpoint("/health"),
      probeHealthEndpoint("/app-health"),
    ]);

    webHealth.value = webStatus;
    serviceHealth.value = serviceStatus;
    lastHealthCheckAt.value = new Date().toISOString();
  } catch (error) {
    webHealth.value = "offline";
    serviceHealth.value = "offline";
    lastHealthCheckAt.value = new Date().toISOString();
  } finally {
    isRefreshingHealth.value = false;
  }
}

async function probeHealthEndpoint(path) {
  try {
    const response = await fetch(path);
    const data = await readResponsePayload(response);
    if (!response.ok) {
      return "offline";
    }

    if (data?.status) {
      return data.status;
    }

    if (typeof data?.raw === "string" && data.raw.trim().toLowerCase() === "ok") {
      return "ok";
    }

    return "unknown";
  } catch (error) {
    return "offline";
  }
}

async function fetchDeployments() {
  try {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(historyFilters.value)) {
      if (value) {
        query.set(key, value);
      }
    }
    query.set("page", String(pagination.value.page));
    query.set("per_page", String(pagination.value.per_page));

    const response = await fetch(`/api/deployments?${query.toString()}`);
    const data = await readResponsePayload(response);
    if (!response.ok) {
      throw new Error(data?.error || `Failed to load deployments (${response.status})`);
    }

    deployments.value = Array.isArray(data?.items) ? data.items : [];
    pagination.value = {
      page: data?.page || 1,
      per_page: data?.per_page || 10,
      total: data?.total || 0,
      pages: data?.pages || 0,
      has_next: Boolean(data?.has_next),
      has_prev: Boolean(data?.has_prev),
    };
    errorMessage.value = "";
  } catch (error) {
    errorMessage.value = error.message;
  }
}

async function createDeployment() {
  isSubmitting.value = true;
  errorMessage.value = "";

  try {
    const response = await fetch("/api/deployments", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(form.value),
    });

    const data = await readResponsePayload(response);
    if (!response.ok) {
      throw new Error(data?.error || `Failed to create deployment (${response.status})`);
    }
    if (!data) {
      throw new Error("Create deployment returned an empty response body");
    }

    deployments.value = [data, ...deployments.value];
    pagination.value.total += 1;
    form.value = {
      application_name: "",
      version: "",
      environment: "staging",
      status: "pending",
    };
    fetchDeployments();
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    isSubmitting.value = false;
  }
}

async function updateDeploymentStatus(deploymentId, status) {
  updatingDeploymentIds.value = {
    ...updatingDeploymentIds.value,
    [deploymentId]: true,
  };
  errorMessage.value = "";

  try {
    const response = await fetch(`/api/deployments/${deploymentId}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ status }),
    });

    const data = await readResponsePayload(response);
    if (!response.ok) {
      throw new Error(data?.error || `Failed to update deployment (${response.status})`);
    }
    if (!data) {
      throw new Error("Update deployment returned an empty response body");
    }

    deployments.value = deployments.value.map((deployment) =>
      deployment.id === deploymentId ? data : deployment,
    );
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    updatingDeploymentIds.value = {
      ...updatingDeploymentIds.value,
      [deploymentId]: false,
    };
  }
}

async function deleteDeployment(deploymentId) {
  deletingDeploymentIds.value = {
    ...deletingDeploymentIds.value,
    [deploymentId]: true,
  };
  errorMessage.value = "";

  try {
    const response = await fetch(`/api/deployments/${deploymentId}`, {
      method: "DELETE",
    });
    const data = await readResponsePayload(response);
    if (!response.ok) {
      throw new Error(data?.error || `Failed to delete deployment (${response.status})`);
    }

    const nextTotal = Math.max(pagination.value.total - 1, 0);
    const nextPages = Math.max(Math.ceil(nextTotal / pagination.value.per_page), 1);
    if (pagination.value.page > nextPages) {
      pagination.value.page = nextPages;
    }

    await fetchDeployments();
  } catch (error) {
    errorMessage.value = error.message;
  } finally {
    deletingDeploymentIds.value = {
      ...deletingDeploymentIds.value,
      [deploymentId]: false,
    };
  }
}

onMounted(async () => {
  await Promise.all([fetchHealth(), fetchDeployments()]);
});

function mapHealthTone(value) {
  if (value === "ok" || value === "reachable") {
    return "success";
  }

  if (value === "checking") {
    return "warning";
  }

  return "danger";
}

function formatHealthLabel(value) {
  if (value === "ok") {
    return "OK (GREEN)";
  }

  if (value === "checking") {
    return "CHECKING";
  }

  return "OFFLINE (RED)";
}

function formatTimestamp(value) {
  return new Date(value).toLocaleString();
}

function formatLastChecked(value) {
  if (!value) {
    return "never";
  }

  return new Date(value).toLocaleTimeString();
}

function isUpdatingDeployment(deploymentId) {
  return Boolean(updatingDeploymentIds.value[deploymentId]);
}

function isDeletingDeployment(deploymentId) {
  return Boolean(deletingDeploymentIds.value[deploymentId]);
}

function canTransitionTo(deployment, status) {
  if (deployment.status === status) {
    return false;
  }

  return (deployment.allowed_transitions || []).includes(status);
}

function resetHistoryFilters() {
  pagination.value.page = 1;
  historyFilters.value = {
    application_name: "",
    environment: "",
    status: "",
  };
  fetchDeployments();
}

function applyFilters() {
  pagination.value.page = 1;
  fetchDeployments();
}

function goToPage(page) {
  if (page < 1 || page === pagination.value.page) {
    return;
  }

  pagination.value.page = page;
  fetchDeployments();
}
</script>

<template>
  <main class="screen">
    <div class="scanlines"></div>

    <section class="hero">
      <p class="eyebrow">DEMO WORKLOAD</p>
      <div class="hero-title">
        <h1>Deployment Notes</h1>
        <div class="python-mark">PY</div>
      </div>
      <p class="lede">
        Track deployment status, validate backend connectivity, and exercise the app like a
        lightweight platform smoke test.
      </p>
    </section>

    <section class="terminal-grid">
      <article class="terminal-panel">
        <div class="terminal-head health-head">
          <div>
            <span class="prompt prompt-green">monitor@server:~$ </span>
            <span class="prompt">health_check</span>
          </div>
          <button class="refresh-button" type="button" :disabled="isRefreshingHealth" @click="fetchHealth">
            {{ isRefreshingHealth ? "CHECKING" : "REFRESH_HEALTH" }} 
          </button>
        </div>

        <div class="health-box">
          <div class="divider"></div>
          <p class="health-line">
            [ SERVICE:
            <strong :data-tone="serviceStatusTone">{{ serviceStatusLabel }}</strong>
            ]
          </p>
          <p class="health-line">
            [ WEB TIER:
            <strong :data-tone="webStatusTone">{{ webStatusLabel }}</strong>
            ]
          </p>
          <div class="divider"></div>
          <p class="log-line">
            ...Service:
            <span :data-tone="serviceStatusTone">{{ serviceHealth === "ok" ? "200 OK" : "Unavailable" }}</span>
          </p>
          <p class="log-line">
            ...Web Tier:
            <span :data-tone="webStatusTone">{{ webHealth === "ok" ? "200 OK" : "Unavailable" }}</span>
          </p>
          <p class="log-line">
            ...Route: <span :data-tone="serviceStatusTone">/api proxied by web tier</span>
          </p>
          <p class="log-line health-details">...Last Check: {{ formatLastChecked(lastHealthCheckAt) }}</p>
        </div>
      </article>

      <article class="terminal-panel">
        <div class="terminal-head">
          <span class="prompt prompt-green">deploy@server:~$ </span>
          <span class="prompt">new_deployment</span>
        </div>

        <form class="terminal-form" @submit.prevent="createDeployment">
          <label class="terminal-row">
            <span class="row-label">&gt; Enter Application Name:</span>
            <input v-model.trim="form.application_name" placeholder=" " required />
          </label>
          <label class="terminal-row">
            <span class="row-label">&gt; Enter Version:</span>
            <input v-model.trim="form.version" placeholder=" " required />
          </label>
          <label class="terminal-row terminal-select-row">
            <span class="row-label">Select Environment:</span>
            <select v-model="form.environment">
              <option value="development">Development</option>
              <option value="staging">Staging</option>
              <option value="production">Production</option>
            </select>
          </label>
          <label class="terminal-row terminal-select-row">
            <span class="row-label">Select Initial Status:</span>
            <select v-model="form.status">
              <option value="pending">pending</option>
              <option value="building">building</option>
              <option value="deployed">deployed</option>
              <option value="failed">failed</option>
            </select>
          </label>
          <button class="terminal-button" type="submit" :disabled="isSubmitting">
             {{ isSubmitting ? "SAVING" : "CREATE_DEPLOYMENT" }} 
          </button>
        </form>
        <p v-if="errorMessage" class="terminal-error">Error: {{ errorMessage }}</p>
      </article>
    </section>

    <section class="history-terminal">
      <div class="terminal-head history-head">
        <div>
          <span class="prompt prompt-green">history@server:~$ </span>
          <span class="prompt">show_history</span>
        </div>
        <button class="refresh-button" type="button" @click="fetchDeployments">REFRESH</button>
      </div>

      <div class="history-filters">
        <label class="filter-field">
          <span>APP</span>
          <input
            v-model.trim="historyFilters.application_name"
            placeholder="app-name"
            @input="applyFilters"
          />
        </label>
        <label class="filter-field">
          <span>ENV</span>
          <select v-model="historyFilters.environment" @change="applyFilters">
            <option value="">all</option>
            <option value="development">development</option>
            <option value="staging">staging</option>
            <option value="production">production</option>
          </select>
        </label>
        <label class="filter-field">
          <span>STATUS</span>
          <select v-model="historyFilters.status" @change="applyFilters">
            <option value="">all</option>
            <option value="pending">pending</option>
            <option value="building">building</option>
            <option value="deployed">deployed</option>
            <option value="failed">failed</option>
          </select>
        </label>
        <button class="clear-filters-button" type="button" @click="resetHistoryFilters">
          clear_filters
        </button>
      </div>

      <div class="history-table">
        <div class="history-columns">
          <span>TIMESTAMP</span>
          <span>APPLICATION</span>
          <span>VERSION</span>
          <span>ENV</span>
          <span>STATUS</span>
        </div>
        <div class="divider"></div>
        <div v-if="sortedDeployments.length" class="history-rows">
          <div v-for="deployment in sortedDeployments" :key="deployment.id" class="history-row">
            <span>{{ formatTimestamp(deployment.created_at) }}</span>
            <span>{{ deployment.application_name }}</span>
            <span>{{ deployment.version }}</span>
            <span>{{ deployment.environment }}</span>
            <div class="status-cell">
              <span :data-status="deployment.status">{{ deployment.status }}</span>
              <button
                class="delete-action"
                type="button"
                :disabled="isDeletingDeployment(deployment.id)"
                @click="deleteDeployment(deployment.id)"
              >
                {{ isDeletingDeployment(deployment.id) ? "[ DELETING ]" : "[ DELETE ]" }}
              </button>
              <div class="status-actions">
                <button
                  v-for="status in statusOptions"
                  :key="status"
                  class="status-action"
                  type="button"
                  :data-active="deployment.status === status"
                  :disabled="
                    !canTransitionTo(deployment, status) ||
                    isUpdatingDeployment(deployment.id) ||
                    isDeletingDeployment(deployment.id)
                  "
                  @click="updateDeploymentStatus(deployment.id, status)"
                >
                  {{ status }}
                </button>
              </div>
            </div>
          </div>
        </div>
        <p v-else class="history-empty">No deployments recorded yet.</p>
      </div>

      <div class="history-pagination">
        <p class="pagination-summary">
          Page {{ pagination.page }} / {{ pagination.pages || 1 }} · {{ pagination.total }} deployments
        </p>
        <div class="pagination-actions">
          <button
            class="pagination-button"
            type="button"
            :disabled="!pagination.has_prev"
            @click="goToPage(pagination.page - 1)"
          >
            < PREV 
          </button>
          <button
            class="pagination-button"
            type="button"
            :disabled="!pagination.has_next"
            @click="goToPage(pagination.page + 1)"
          >
             NEXT >
          </button>
        </div>
      </div>
    </section>
  </main>
</template>
