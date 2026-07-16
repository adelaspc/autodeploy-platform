import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path

from control_plane.deployment_spec import project_for_deployment
from control_plane.security import env_var_is_secret, redact_text, secret_values_from_env_vars
from worker.execution.contracts import ExecutionResult, WorkerExecutionError
from worker.services.healthcheck import DockerHealthcheckServiceMixin


class DockerRuntimeMixin(DockerHealthcheckServiceMixin):
    def deploy(self, deployment):
        project = project_for_deployment(deployment)
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "deploy.log"
        runtime_log_path = logs_dir / "runtime.log"
        container_name = self._container_name(deployment)
        env_args, secret_values = self._env_args_with_redaction(deployment)

        self._remove_container_if_exists(container_name)
        run_result = None
        for port_attempt in range(1, 4):
            host_port = self.port_allocator()
            published_port = f"{self.deploy_host}:{host_port}:{project.port}"
            try:
                run_result = self._run_command(
                    "deploy.container_start",
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        container_name,
                        "--publish",
                        published_port,
                        *env_args,
                        deployment.build.image_tag,
                    ],
                    log_path=log_path,
                    redacted_values=secret_values,
                )
                break
            except WorkerExecutionError as exc:
                if port_attempt == 3 or not self._is_port_allocation_error(exc):
                    raise
                self._remove_container_if_exists(container_name)
        service_url = f"http://{self.deploy_host}:{host_port}"
        healthcheck_url = f"{service_url}{project.healthcheck_path}"
        run_result.metadata["port_allocation_attempts"] = port_attempt
        container_id = (run_result.metadata.get("output_tail") or [run_result.message])[-1]

        try:
            health_metadata = self._wait_for_healthcheck(healthcheck_url, log_path, redacted_values=secret_values)
        except WorkerExecutionError as exc:
            runtime_metadata = self._capture_container_logs(
                deployment,
                container_name,
                runtime_log_path,
                step="deploy.container_logs",
                missing_ok=True,
            )
            if not runtime_metadata.get("runtime_log_summary") and exc.metadata.get("healthcheck_last_summary"):
                runtime_metadata["runtime_log_summary"] = exc.metadata["healthcheck_last_summary"]
                runtime_metadata["runtime_output_tail"] = [exc.metadata["healthcheck_last_summary"][:240]]
            self._remove_container_if_exists(container_name)
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata={
                    **exc.metadata,
                    **runtime_metadata,
                    "container_name": container_name,
                    "container_id": container_id,
                    "host_port": host_port,
                    "service_url": service_url,
                    "healthcheck_url": healthcheck_url,
                },
                log_path=exc.log_path,
            ) from exc

        runtime_metadata = self._capture_container_logs(
            deployment,
            container_name,
            runtime_log_path,
            step="deploy.container_logs",
            missing_ok=True,
        )

        return ExecutionResult(
            "Container started and passed healthcheck.",
            metadata={
                "executor": self.deploy_target,
                "container_name": container_name,
                "container_id": container_id,
                "host_port": host_port,
                "published_port": published_port,
                "healthcheck_url": healthcheck_url,
                "port_allocation_attempts": port_attempt,
                **runtime_metadata,
                **health_metadata,
            },
            log_path=str(log_path),
            service_url=service_url,
            deploy_target=self.deploy_target,
            container_name=container_name,
            container_id=container_id,
            host_port=host_port,
            healthcheck_url=healthcheck_url,
            runtime_log_path=str(runtime_log_path) if runtime_metadata.get("runtime_log_path") else None,
        )

    def stop(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "stop.log"
        runtime_log_path = logs_dir / "runtime.log"
        container_name = deployment.container_name or self._container_name(deployment)
        runtime_metadata = self._capture_container_logs(
            deployment,
            container_name,
            runtime_log_path,
            step="deploy.container_logs",
            missing_ok=True,
        )
        result = self._run_command(
            "deploy.container_stop",
            ["docker", "rm", "--force", container_name],
            log_path=log_path,
        )
        return ExecutionResult(
            "Container removed successfully.",
            metadata=result.metadata
            | {
                "executor": self.deploy_target,
                "stopped": True,
                "container_name": container_name,
                **runtime_metadata,
            },
            log_path=str(log_path),
            deploy_target=self.deploy_target,
            container_name=container_name,
            runtime_log_path=str(runtime_log_path) if runtime_metadata.get("runtime_log_path") else None,
        )

    def container_exists(self, deployment):
        container_name = deployment.container_name or self._container_name(deployment)
        result = self._execute_command(
            ["docker", "container", "inspect", container_name],
            allow_heartbeat=False,
        )
        return result.returncode == 0

    def cleanup_workspace(self, deployment):
        workspace_removed = False
        log_removed = False
        removed_paths = []

        workspace_path = getattr(deployment.build, "workspace_path", None)
        log_path = getattr(deployment.build, "log_path", None)
        build_log_path = getattr(deployment.build, "build_log_path", None)
        workspace_dir = self._validated_cleanup_path(workspace_path) if workspace_path else None
        log_file = self._validated_cleanup_path(log_path) if log_path else None
        build_log_file = self._validated_cleanup_path(build_log_path) if build_log_path else None

        if workspace_dir:
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
                workspace_removed = True
                removed_paths.append(str(workspace_dir))

        if log_file:
            if log_file.exists():
                log_file.unlink()
                log_removed = True
                removed_paths.append(str(log_file))

        if build_log_file and build_log_file != log_file:
            if build_log_file.exists():
                build_log_file.unlink()
                log_removed = True
                removed_paths.append(str(build_log_file))

        return {
            "workspace_removed": workspace_removed,
            "log_removed": log_removed,
            "removed_paths": removed_paths,
        }

    def _validated_cleanup_path(self, path):
        workspace_root = self.workspace_root.resolve()
        candidate = Path(path).resolve()
        if candidate == workspace_root or workspace_root not in candidate.parents:
            raise WorkerExecutionError(
                "deployment.cleanup",
                "Refusing to remove a path outside the configured workspace root",
                metadata={"workspace_root": str(workspace_root)},
            )
        return candidate

    def _prepare_workspace(self, deployment):
        workspace_dir = self.workspace_root / f"project-{deployment.project_id}" / f"deployment-{deployment.id}"
        repo_dir = workspace_dir / "repo"
        logs_dir = workspace_dir / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkerExecutionError(
                "repository.workspace",
                f"Unable to prepare deployment workspace '{workspace_dir}': {exc}",
                metadata={
                    "workspace_path": str(workspace_dir),
                    "workspace_root": str(self.workspace_root),
                },
            ) from exc
        return workspace_dir, repo_dir, logs_dir

    @staticmethod
    def _container_name(deployment):
        project = project_for_deployment(deployment)
        safe_project_name = "".join(char if char.isalnum() or char == "-" else "-" for char in project.name.lower())
        return f"paas-{safe_project_name}-{deployment.id}"

    @staticmethod
    def _allocate_host_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _is_port_allocation_error(error):
        output = " ".join(
            [error.message, *((error.metadata or {}).get("output_tail") or [])]
        ).lower()
        return any(marker in output for marker in ("port is already allocated", "address already in use", "bind:"))

    def _capture_container_logs(self, deployment, container_name, log_path, *, step, missing_ok):
        result = self._execute_command(["docker", "logs", container_name], allow_heartbeat=False)
        combined_output = redact_text(
            (result.stdout or "") + (result.stderr or ""),
            secret_values=secret_values_from_env_vars(project_for_deployment(deployment).env_vars),
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(combined_output, encoding="utf-8")

        if result.returncode != 0 and not missing_ok:
            metadata = self._build_command_metadata(
                args=["docker", "logs", container_name],
                output=combined_output,
                started_at=datetime.now(timezone.utc),
                returncode=result.returncode,
                attempt=1,
                total_attempts=1,
            )
            raise WorkerExecutionError(
                step,
                metadata["summary"] or "Failed to capture container logs",
                metadata=metadata | {"runtime_log_path": str(log_path)},
                log_path=str(log_path),
            )

        return {
            "runtime_log_path": str(log_path),
            "runtime_log_summary": self._summarize_output(combined_output),
            "runtime_output_tail": self._tail_lines(combined_output),
            "runtime_log_missing": result.returncode != 0,
        }

    def _remove_container_if_exists(self, container_name):
        self._execute_command(["docker", "rm", "--force", container_name], allow_heartbeat=False)

    @staticmethod
    def _env_args_with_redaction(deployment):
        project = project_for_deployment(deployment)
        args = []
        redacted_values = []
        for item in project.env_vars or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            args.extend(["--env", f"{name}={value}"])
            if env_var_is_secret(item):
                redacted_values.append(str(value))
        return args, tuple(redacted_values)
