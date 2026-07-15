export function formatTime(value) {
  return value ? new Date(value).toLocaleString() : "not recorded";
}

export function shortSha(value) {
  return value ? value.slice(0, 12) : "unknown";
}

export function statusTone(status) {
  if (["ok", "running", "succeeded", "reachable", "healthy"].includes(status)) return "success";
  if (["pending", "cloning", "building", "testing", "pushing_image", "deploying"].includes(status)) return "warning";
  if (["failed", "degraded", "error"].includes(status)) return "danger";
  return "neutral";
}
