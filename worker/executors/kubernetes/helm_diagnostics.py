"""Capture bounded workload evidence without replacing the original Helm failure."""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from control_plane.deployment_spec import project_for_deployment
from control_plane.security import redact_log_text, redact_sensitive_data, secret_values_from_env_vars


class FailureSnapshot:
    """Keep command failures separate from successful diagnostic output."""

    def __init__(self, executor, deployment, logs_dir):
        self.executor = executor
        self.logs_dir = logs_dir
        self.deadline = time.monotonic() + 30
        self.errors = []
        self.successes = 0
        self.metadata = {"diagnostics_collected_at": datetime.now(timezone.utc).isoformat()}
        self.secret_values = helm_failure_secret_values(executor, deployment)

    def error(self, operation, reason):
        if len(self.errors) < 12:
            self.errors.append({"operation": operation, "reason": reason})

    def command(self, operation, *parts, json_output=False):
        # Ownership/cancellation exceptions must escape, unlike diagnostic failures.
        if self.executor.heartbeat is not None:
            self.executor.heartbeat()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.error(operation, "collection_budget_exhausted")
            return None
        args = self.executor._kubectl_args(*parts)
        try:
            result = self.executor._execute_command(args, timeout=min(5, remaining))
        except subprocess.TimeoutExpired:
            self.error(operation, "command_timed_out")
            return None
        except OSError:
            self.error(operation, "command_unavailable")
            return None

        if result.returncode:
            output = (result.stderr or "") + (result.stdout or "")
            lower = output.lower()
            reason = "command_failed"
            if "forbidden" in lower or "unauthorized" in lower:
                reason = "access_denied"
            elif "--previous" in parts and "container" in lower:
                reason = "previous_logs_unavailable"
            elif "notfound" in lower or "not found" in lower:
                reason = "resource_not_found"
            self.error(operation, reason)
            self.write(operation, args, output)
            return None

        output = result.stdout or ""
        if json_output:
            try:
                payload = json.loads(output)
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise ValueError("Expected a Kubernetes list")
            except (ValueError, TypeError):
                self.error(operation, "invalid_resource_response")
                return None
            # Do not retain a full Pod spec: it may contain literal environment values.
            return payload
        self.successes += 1
        self.write(operation, args, output)
        return redact_log_text(output, secret_values=self.secret_values)

    def write(self, operation, args, output):
        path = self.logs_dir / f"kubernetes-{operation}.log"
        try:
            self.executor._write_log(
                path, args, redact_log_text(output, secret_values=self.secret_values),
                redacted_values=self.secret_values,
            )
        except OSError:
            self.error(operation, "log_write_failed")

    def finish(self):
        self.metadata.update({
            "diagnostics_collection_status": (
                "unavailable" if not self.successes else "partial" if self.errors else "complete"
            ),
            "diagnostics_collection_errors": self.errors,
        })
        return redact_sensitive_data(self.metadata, secret_values=self.secret_values)


def helm_failure_secret_values(executor, deployment):
    return secret_values_from_env_vars(project_for_deployment(deployment).env_vars) + tuple(
        value for key, value in os.environ.items()
        if key.startswith(("CONTROL_PLANE_GIT_TOKEN_", "CONTROL_PLANE_API_TOKEN_"))
        or key in {"CONTROL_PLANE_REGISTRY_PASSWORD", "CONTROL_PLANE_METRICS_TOKEN", "CONTROL_PLANE_GITHUB_WEBHOOK_SECRET"}
    ) + tuple(value for value in (executor.registry_password,) if value)


def collect_helm_failure_diagnostics(executor, deployment, *, deployment_name, service_name, pod_selector, logs_dir):
    snapshot = FailureSnapshot(executor, deployment, logs_dir)
    try:
        _collect(snapshot, deployment_name=deployment_name, service_name=service_name, pod_selector=pod_selector)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        # Invalid diagnostic data or local I/O must not hide the original failure.
        # Deliberately exclude ownership and cancellation exceptions.
        snapshot.error("helm-snapshot", "collection_failed")
    return snapshot.finish()


def _collect(snapshot, *, deployment_name, service_name, pod_selector):
    executor, logs_dir = snapshot.executor, snapshot.logs_dir
    payload = snapshot.command("helm-pods", "get", "pods", "-l", pod_selector, "-o", "json", json_output=True)
    items = []
    if payload is not None:
        label_key, label_value = pod_selector.split("=", 1)
        # Kubernetes filters the query; also constrain what we subsequently inspect.
        for item in payload["items"]:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") or {}
            name = metadata.get("name", "")
            if ((metadata.get("labels") or {}).get(label_key) == label_value
                    and metadata.get("namespace", executor.namespace) == executor.namespace
                    and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)):
                items.append(item)
        if not items:
            snapshot.error("helm-pods", "no_matching_pods")
        if len(items) > 3:
            snapshot.error("helm-pods", "pod_limit_reached")
        items = items[:3]
        if items:
            snapshot.successes += 1
            runtime = executor._pod_runtime_metadata(
                items, prefix="helm", pod_json_log_path=logs_dir / "kubernetes-helm-pod-runtime.log",
            )
            snapshot.metadata.update(runtime)
            snapshot.write("helm-pod-runtime", [], json.dumps(runtime))

    # Prioritize application output over resource descriptions within the budget.
    summaries = {"pod_logs": [], "pod_previous_logs": [], "pod_describe": []}
    for item in items:
        name = item["metadata"]["name"]
        for kind, extra in (("pod_logs", ()), ("pod_previous_logs", ("--previous",))):
            output = snapshot.command(f"helm-{kind}-{name}", "logs", f"pod/{name}", "-c", "app", *extra, "--tail", "50")
            if output:
                summaries[kind].append(f"{name}:\n" + "\n".join(line[:240] for line in output.splitlines()[-50:]))
    for item in items:
        name = item["metadata"]["name"]
        output = snapshot.command(f"helm-describe-{name}", "describe", f"pod/{name}")
        if output:
            summaries["pod_describe"].append(f"{name}:\n" + output[-4000:])
    for kind, chunks in summaries.items():
        snapshot.metadata[f"helm_{kind}_summary"] = "\n\n".join(chunks)[:4000] or None
    for kind, name in (("deployment", deployment_name), ("service", service_name)):
        output = snapshot.command(f"helm-describe-{kind}", "describe", f"{kind}/{name}")
        snapshot.metadata[f"helm_{kind}_summary"] = output[-4000:] if output else None
