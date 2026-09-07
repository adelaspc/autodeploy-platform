let inMemoryToken = "";

export function storedToken() {
  return inMemoryToken;
}

export function storeToken(token) {
  // Normalize user input so accidental surrounding whitespace is not sent as
  // part of the Bearer token.
  const normalized = (token || "").trim();
  inMemoryToken = normalized;
}

export async function apiRequest(path, options = {}) {
  const token = storedToken();

  // Start with any caller-provided headers. Add JSON and authentication
  // defaults only when appropriate, keeping this helper usable for GET requests
  // and for callers that need to specify another content type.
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
    // Convert every non-successful HTTP response into the same Error shape so
    // Vue components can display a message and still inspect status/payload.
    const message = payload?.error || payload?.message || `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function readPayload(response) {
  // Read the body once as text: empty responses become null, JSON responses are
  // parsed, and plain-text responses remain available in a predictable object.
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
