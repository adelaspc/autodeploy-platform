const TOKEN_STORAGE_KEY = "autodeploy-control-plane-token";

export function storedToken() {
  return window.localStorage.getItem(TOKEN_STORAGE_KEY) || "";
}

export function storeToken(token) {
  const normalized = (token || "").trim();
  if (normalized) {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, normalized);
  } else {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

export async function apiRequest(path, options = {}) {
  const token = storedToken();
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(path, { ...options, headers });
  const payload = await readPayload(response);
  if (!response.ok) {
    const message = payload?.error || payload?.message || `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function readPayload(response) {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text);
  } catch {
    return { content: text };
  }
}
