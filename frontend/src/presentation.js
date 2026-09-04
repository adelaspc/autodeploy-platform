export function parseApiTimestamp(value) {
  if (!value) return null;
  if (typeof value !== "string") return new Date(value);

  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  return new Date(hasTimezone ? value : `${value}Z`);
}

export function formatTime(value) {
  return value ? parseApiTimestamp(value).toLocaleString() : "not recorded";
}

export function shortSha(value) {
  return value ? value.slice(0, 12) : "unknown";
}

export function statusTone(status) {
  if (["ok", "running", "succeeded", "reachable", "healthy"].includes(status)) return "success";
  if (["pending", "cloning", "building", "testing", "pushing_image", "deploying", "cancelled"].includes(status)) return "warning";
  if (["failed", "degraded", "error"].includes(status)) return "danger";
  return "neutral";
}
