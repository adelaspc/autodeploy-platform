import json
import subprocess

from control_plane.deployment_spec import project_for_deployment
from control_plane.security import redact_text, secret_values_from_env_vars


class KubernetesDiagnosticsMixin:
    def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile"):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        deployment_name = self._k8s_deployment_name(deployment)
        return self._collect_pod_diagnostics(deployment_name, prefix=prefix, logs_dir=logs_dir)

    def _capture_runtime_logs(self, deployment, deployment_name, *, logs_dir):
        """Persist a best-effort snapshot of the workload's current stdout/stderr."""
        runtime_log_path = logs_dir / "runtime.log"
        args = self._kubectl_args(
            "logs",
            f"deployment/{deployment_name}",
            "--all-containers=true",
            "--prefix=true",
            "--tail=200",
        )
        try:
            completed = self._execute_command(args, allow_heartbeat=False)
            output = (completed.stdout or "") + (completed.stderr or "")
        except Exception as exc:
            output = str(exc)

        secret_values = secret_values_from_env_vars(project_for_deployment(deployment).env_vars)
        output = redact_text(self._sanitize_text(output), secret_values=secret_values)
        self._write_log(runtime_log_path, args, output, redacted_values=secret_values)
        return {
            "runtime_log_path": str(runtime_log_path),
            "runtime_log_summary": self._summarize_output(output),
            "runtime_output_tail": self._tail_lines(output),
        }


    def _collect_rollout_diagnostics(self, deployment_name, *, logs_dir):
        pods_log_path = logs_dir / "kubernetes-rollout-pods.log"
        describe_log_path = logs_dir / "kubernetes-rollout-describe.log"
        pods_output = self._run_diagnostic_command(
            self._kubectl_args("get", "pods", "-o", "wide"),
            log_path=pods_log_path,
        )
        describe_output = self._run_diagnostic_command(
            self._kubectl_args("describe", f"deployment/{deployment_name}"),
            log_path=describe_log_path,
        )
        return {
            "rollout_pods_log_path": str(pods_log_path),
            "rollout_pods_summary": self._summarize_output(pods_output),
            "rollout_pods_output_tail": self._tail_lines(pods_output),
            "rollout_describe_log_path": str(describe_log_path),
            "rollout_describe_summary": self._summarize_output(describe_output),
            "rollout_describe_output_tail": self._tail_lines(describe_output),
        } | self._collect_pod_diagnostics(deployment_name, prefix="rollout", logs_dir=logs_dir)

    def _collect_apply_diagnostics(self, *, logs_dir):
        pods_log_path = logs_dir / "kubernetes-apply-pods.log"
        services_log_path = logs_dir / "kubernetes-apply-services.log"
        pods_output = self._run_diagnostic_command(
            self._kubectl_args("get", "pods", "-o", "wide"),
            log_path=pods_log_path,
        )
        services_output = self._run_diagnostic_command(
            self._kubectl_args("get", "services"),
            log_path=services_log_path,
        )
        return {
            "apply_pods_log_path": str(pods_log_path),
            "apply_pods_summary": self._summarize_output(pods_output),
            "apply_pods_output_tail": self._tail_lines(pods_output),
            "apply_services_log_path": str(services_log_path),
            "apply_services_summary": self._summarize_output(services_output),
            "apply_services_output_tail": self._tail_lines(services_output),
        } | self._collect_pod_diagnostics(deployment_name=None, prefix="apply", logs_dir=logs_dir)

    def _collect_healthcheck_diagnostics(self, deployment_name, *, service_name, logs_dir):
        pods_log_path = logs_dir / "kubernetes-healthcheck-pods.log"
        deployment_log_path = logs_dir / "kubernetes-healthcheck-describe-deployment.log"
        service_log_path = logs_dir / "kubernetes-healthcheck-describe-service.log"
        pods_output = self._run_diagnostic_command(
            self._kubectl_args("get", "pods", "-o", "wide"),
            log_path=pods_log_path,
        )
        deployment_output = self._run_diagnostic_command(
            self._kubectl_args("describe", f"deployment/{deployment_name}"),
            log_path=deployment_log_path,
        )
        service_output = self._run_diagnostic_command(
            self._kubectl_args("describe", f"service/{service_name}"),
            log_path=service_log_path,
        )
        return {
            "healthcheck_pods_log_path": str(pods_log_path),
            "healthcheck_pods_summary": self._summarize_output(pods_output),
            "healthcheck_pods_output_tail": self._tail_lines(pods_output),
            "healthcheck_deployment_log_path": str(deployment_log_path),
            "healthcheck_deployment_summary": self._summarize_output(deployment_output),
            "healthcheck_deployment_output_tail": self._tail_lines(deployment_output),
            "healthcheck_service_log_path": str(service_log_path),
            "healthcheck_service_summary": self._summarize_output(service_output),
            "healthcheck_service_output_tail": self._tail_lines(service_output),
        } | self._collect_pod_diagnostics(deployment_name, prefix="healthcheck", logs_dir=logs_dir)

    def _collect_pod_diagnostics(self, deployment_name, *, prefix, logs_dir):
        pod_names_log_path = logs_dir / f"kubernetes-{prefix}-pod-names.log"
        pod_name_args = ["get", "pods"]
        if deployment_name:
            pod_name_args.extend(["-l", f"app.kubernetes.io/instance={deployment_name}"])
        pod_name_args.extend(["-o", "name"])
        pod_names_output = self._run_diagnostic_command(
            self._kubectl_args(*pod_name_args),
            log_path=pod_names_log_path,
        )
        pod_names = [
            line.strip().split("/", 1)[-1]
            for line in pod_names_output.splitlines()
            if line.strip()
        ][:3]

        describe_summaries = []
        logs_summaries = []
        previous_logs_summaries = []
        describe_log_paths = []
        logs_log_paths = []
        previous_logs_log_paths = []
        for pod_name in pod_names:
            describe_log_path = logs_dir / f"kubernetes-{prefix}-describe-{pod_name}.log"
            logs_log_path = logs_dir / f"kubernetes-{prefix}-logs-{pod_name}.log"
            previous_logs_log_path = logs_dir / f"kubernetes-{prefix}-logs-previous-{pod_name}.log"
            describe_output = self._run_diagnostic_command(
                self._kubectl_args("describe", f"pod/{pod_name}"),
                log_path=describe_log_path,
            )
            logs_output = self._run_diagnostic_command(
                self._kubectl_args("logs", f"pod/{pod_name}", "--tail", "50"),
                log_path=logs_log_path,
            )
            previous_logs_output = self._run_diagnostic_command(
                self._kubectl_args("logs", f"pod/{pod_name}", "--previous", "--tail", "50"),
                log_path=previous_logs_log_path,
            )
            describe_log_paths.append(str(describe_log_path))
            logs_log_paths.append(str(logs_log_path))
            previous_logs_log_paths.append(str(previous_logs_log_path))
            describe_summary = self._summarize_output(describe_output)
            logs_summary = self._summarize_output(logs_output)
            previous_logs_summary = self._summarize_output(previous_logs_output)
            if describe_summary:
                describe_summaries.append(f"{pod_name}: {describe_summary}")
            if logs_summary:
                logs_summaries.append(f"{pod_name}: {logs_summary}")
            if previous_logs_summary:
                previous_logs_summaries.append(f"{pod_name}: {previous_logs_summary}")

        return {
            f"{prefix}_pod_names_log_path": str(pod_names_log_path),
            f"{prefix}_pod_names": pod_names,
            f"{prefix}_pod_describe_log_paths": describe_log_paths,
            f"{prefix}_pod_describe_summary": " | ".join(describe_summaries)[:1000] if describe_summaries else None,
            f"{prefix}_pod_logs_log_paths": logs_log_paths,
            f"{prefix}_pod_logs_summary": " | ".join(logs_summaries)[:1000] if logs_summaries else None,
            f"{prefix}_pod_previous_logs_log_paths": previous_logs_log_paths,
            f"{prefix}_pod_previous_logs_summary": " | ".join(previous_logs_summaries)[:1000]
            if previous_logs_summaries
            else None,
        } | self._collect_pod_runtime_metadata(deployment_name, prefix=prefix, logs_dir=logs_dir)

    def _collect_pod_runtime_metadata(self, deployment_name, *, prefix, logs_dir):
        if not deployment_name:
            return {}
        pod_json_log_path = logs_dir / f"kubernetes-{prefix}-pods.json.log"
        try:
            output = self._run_diagnostic_command(
                self._kubectl_args(
                    "get",
                    "pods",
                    "-l",
                    f"app.kubernetes.io/instance={deployment_name}",
                    "-o",
                    "json",
                ),
                log_path=pod_json_log_path,
            )
        except Exception:
            return {}
        try:
            items = json.loads(output).get("items", [])
        except (AttributeError, json.JSONDecodeError):
            items = []
        if not items:
            return {}

        pods = []
        for item in items[:3]:
            spec = item.get("spec") or {}
            status = item.get("status") or {}
            containers_by_name = {
                container.get("name"): container for container in spec.get("containers") or []
            }
            container_details = []
            for container_status in status.get("containerStatuses") or []:
                state = container_status.get("state") or {}
                state_name = next((name for name in ("waiting", "terminated", "running") if state.get(name)), None)
                state_detail = state.get(state_name) or {} if state_name else {}
                spec_container = containers_by_name.get(container_status.get("name")) or {}
                container_details.append(
                    {
                        "name": container_status.get("name"),
                        "image": container_status.get("image") or spec_container.get("image"),
                        "ready": bool(container_status.get("ready")),
                        "restart_count": int(container_status.get("restartCount") or 0),
                        "reason": state_detail.get("reason") or (state_name.title() if state_name else None),
                    }
                )
            pods.append(
                {
                    "name": (item.get("metadata") or {}).get("name"),
                    "phase": status.get("phase"),
                    "containers": container_details,
                    "images": [container.get("image") for container in spec.get("containers") or [] if container.get("image")],
                    "image_pull_secrets": [
                        secret.get("name") for secret in spec.get("imagePullSecrets") or [] if secret.get("name")
                    ],
                }
            )

        primary = pods[0] if pods else {}
        primary_containers = primary.get("containers") or []
        return {
            f"{prefix}_pod_runtime_log_path": str(pod_json_log_path),
            f"{prefix}_pod_runtime": pods,
            f"{prefix}_pod_names": [pod.get("name") for pod in pods if pod.get("name")],
            f"{prefix}_pod_phase": primary.get("phase"),
            f"{prefix}_container_reason": primary_containers[0].get("reason") if primary_containers else None,
            f"{prefix}_restart_count": sum(item.get("restart_count", 0) for item in primary_containers),
            f"{prefix}_images": primary.get("images") or [],
            f"{prefix}_image_pull_secrets": primary.get("image_pull_secrets") or [],
        }

    def _run_diagnostic_command(self, args, *, log_path):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            completed = self._execute_command(args, allow_heartbeat=False)
            output = self._sanitize_text((completed.stdout or "") + (completed.stderr or ""))
        except (subprocess.TimeoutExpired, OSError) as exc:
            output = str(exc)
        self._write_log(log_path, args, output)
        return output

    @staticmethod
    def _terminate_process(process):
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
