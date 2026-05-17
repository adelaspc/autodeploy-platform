from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import time
import os
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from flask import current_app

from backend.security import env_var_is_secret, redact_sensitive_data, redact_text, secret_values_from_env_vars


@dataclass
class ExecutionResult:
    message: str
    metadata: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    log_path: str | None = None
    workspace_path: str | None = None
    image_tag: str | None = None
    image_ref: str | None = None
    service_url: str | None = None
    deploy_target: str | None = None
    container_name: str | None = None
    container_id: str | None = None
    host_port: int | None = None
    healthcheck_url: str | None = None
    runtime_log_path: str | None = None


@dataclass
class PreflightResult:
    status: str
    summary: str
    metadata: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    log_path: str | None = None
    deploy_target: str | None = None


@dataclass(frozen=True)
class ExecutorContract:
    name: str
    deploy_target: str
    runtime: str
    healthcheck_strategy: str
    requires_registry_push: bool = False
    supports_runtime_logs: bool = False
    supports_runtime_reconciliation: bool = False
    managed_resources: tuple[str, ...] = ()
    required_config: tuple[str, ...] = ()
    optional_config: tuple[str, ...] = ()

    def as_dict(self):
        return {
            "name": self.name,
            "deploy_target": self.deploy_target,
            "runtime": self.runtime,
            "healthcheck_strategy": self.healthcheck_strategy,
            "requires_registry_push": self.requires_registry_push,
            "supports_runtime_logs": self.supports_runtime_logs,
            "supports_runtime_reconciliation": self.supports_runtime_reconciliation,
            "managed_resources": list(self.managed_resources),
            "required_config": list(self.required_config),
            "optional_config": list(self.optional_config),
        }


class WorkerExecutionError(Exception):
    def __init__(self, step, message, *, metadata=None, log_path=None, events=None):
        super().__init__(message)
        self.step = step
        self.message = message
        self.metadata = metadata or {}
        self.log_path = log_path
        self.events = events or []


class DeploymentExecutor:
    deploy_target = "unknown"
    contract = ExecutorContract(
        name="unknown",
        deploy_target="unknown",
        runtime="unknown",
        healthcheck_strategy="unknown",
    )

    @classmethod
    def contract_spec(cls):
        return cls.contract

    def set_heartbeat(self, heartbeat):
        self.heartbeat = heartbeat

    def clone_repo(self, deployment):
        raise NotImplementedError

    def build_image(self, deployment):
        raise NotImplementedError

    def run_tests(self, deployment):
        raise NotImplementedError

    def tag_image(self, deployment):
        raise NotImplementedError

    def push_image(self, deployment):
        raise NotImplementedError

    def preflight_deploy(self, deployment):
        return PreflightResult(
            status="skipped",
            summary="Deployment preflight is not required for this executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            deploy_target=self.deploy_target,
        )

    def deploy(self, deployment):
        raise NotImplementedError

    def stop(self, deployment):
        raise NotImplementedError

    def container_exists(self, deployment):
        return False

    def cleanup_workspace(self, deployment):
        return {}

    def runtime_resource_status(self, deployment):
        return {}

    def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile"):
        return {}


class FakeDeploymentExecutor(DeploymentExecutor):
    deploy_target = "fake"
    contract = ExecutorContract(
        name="fake",
        deploy_target="fake",
        runtime="simulated",
        healthcheck_strategy="simulated",
    )

    def clone_repo(self, deployment):
        return ExecutionResult(
            "Repository cloned",
            metadata={"executor": self.deploy_target},
            workspace_path=f"/tmp/paas-workspaces/fake/deployment-{deployment.id}",
        )

    def build_image(self, deployment):
        image_tag = deployment.build.image_tag or deployment.build.commit_sha[:12]
        image_name = deployment.build.image_name or deployment.project.name
        image_ref = deployment.build.image_ref or f"local/{image_name}:{image_tag}"
        return ExecutionResult(
            "Docker image built",
            metadata={"executor": self.deploy_target},
            image_tag=image_tag,
            image_ref=image_ref,
        )

    def run_tests(self, deployment):
        return ExecutionResult("Test command completed", metadata={"executor": self.deploy_target})

    def tag_image(self, deployment):
        return ExecutionResult(
            "Image tag skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            image_tag=deployment.build.image_tag or f"{deployment.project.name}:{deployment.id}",
            image_ref=deployment.build.image_ref or f"{deployment.project.name}:{deployment.id}",
        )

    def push_image(self, deployment):
        return ExecutionResult(
            "Image push skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
        )

    def preflight_deploy(self, deployment):
        return PreflightResult(
            status="skipped",
            summary="Deployment preflight skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            deploy_target=self.deploy_target,
        )

    def deploy(self, deployment):
        return ExecutionResult(
            "Deployment marked as running",
            metadata={"executor": self.deploy_target},
            service_url=deployment.service_url or f"https://{deployment.project.name}.local",
            deploy_target=self.deploy_target,
        )

    def stop(self, deployment):
        return ExecutionResult(
            "Deployment marked as stopped",
            metadata={"executor": self.deploy_target, "stopped": True},
            deploy_target=self.deploy_target,
            container_name=deployment.container_name,
            container_id=deployment.container_id,
        )

    def cleanup_workspace(self, deployment):
        return {"workspace_removed": False, "log_removed": False}


class LocalDockerExecutor(DeploymentExecutor):
    deploy_target = "local-docker"
    contract = ExecutorContract(
        name="local-docker",
        deploy_target="local-docker",
        runtime="container",
        healthcheck_strategy="direct-http",
        supports_runtime_logs=True,
        supports_runtime_reconciliation=True,
        managed_resources=("docker-image", "docker-container"),
        optional_config=(
            "CONTROL_PLANE_REGISTRY_ENABLED",
            "CONTROL_PLANE_REGISTRY_URL",
            "CONTROL_PLANE_REGISTRY_NAMESPACE",
            "CONTROL_PLANE_REGISTRY_USERNAME",
            "CONTROL_PLANE_REGISTRY_PASSWORD",
            "CONTROL_PLANE_DEPLOY_HOST",
            "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
            "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
            "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
        ),
    )
    retryable_steps = frozenset({"repository.clone", "image.build", "tests", "image.push"})

    def __init__(
        self,
        workspace_root,
        command_timeout,
        runner=None,
        retry_count=1,
        sleep_fn=None,
        port_allocator=None,
        health_probe=None,
        deploy_host="127.0.0.1",
        healthcheck_timeout=30,
        healthcheck_interval=1,
        heartbeat_interval=30,
        registry_enabled=False,
        registry_url=None,
        registry_namespace=None,
        registry_username=None,
        registry_password=None,
        popen_factory=None,
    ):
        self.workspace_root = Path(workspace_root)
        self.command_timeout = command_timeout
        self.runner = runner
        self.popen_factory = popen_factory or subprocess.Popen
        self.retry_count = retry_count
        self.sleep_fn = sleep_fn or time.sleep
        self.port_allocator = port_allocator or self._allocate_host_port
        self.health_probe = health_probe or self._default_health_probe
        self.deploy_host = deploy_host
        self.healthcheck_timeout = healthcheck_timeout
        self.healthcheck_interval = healthcheck_interval
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat = None
        self.registry_enabled = registry_enabled
        self.registry_url = (registry_url or "").strip().rstrip("/")
        self.registry_namespace = (registry_namespace or "").strip().strip("/")
        self.registry_username = registry_username
        self.registry_password = registry_password

    def clone_repo(self, deployment):
        workspace_dir, repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "clone.log"

        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        clone_args = [
            "git",
            "clone",
            "--branch",
            deployment.project.branch,
            "--single-branch",
            deployment.project.repo_url,
            str(repo_dir),
        ]
        clone_env, redacted_values = self._git_clone_environment(deployment)
        result = self._run_command(
            "repository.clone",
            clone_args,
            log_path=log_path,
            env=clone_env,
            redacted_values=redacted_values,
        )
        result.workspace_path = str(workspace_dir)
        return result

    def build_image(self, deployment):
        workspace_dir, repo_dir, logs_dir = self._prepare_workspace(deployment)
        dockerfile_path = repo_dir / deployment.project.dockerfile_path
        build_context_path = repo_dir / deployment.project.build_context

        if not dockerfile_path.is_file():
            raise WorkerExecutionError(
                "image.build",
                f"Dockerfile not found at '{dockerfile_path}'",
                metadata={"dockerfile_path": str(dockerfile_path)},
            )

        if not build_context_path.exists():
            raise WorkerExecutionError(
                "image.build",
                f"Build context not found at '{build_context_path}'",
                metadata={"build_context": str(build_context_path)},
            )

        image_name = self._image_name(deployment)
        tag_suffix = self._tag_suffix(deployment)
        image_tag = f"{image_name}:{tag_suffix}"
        image_ref = self._registry_image_ref(image_name, tag_suffix) or image_tag
        log_path = logs_dir / "build.log"
        result = self._run_command(
            "image.build",
            [
                "docker",
                "build",
                "--tag",
                image_tag,
                "--file",
                str(dockerfile_path),
                str(build_context_path),
            ],
            log_path=log_path,
        )
        result.workspace_path = str(workspace_dir)
        result.image_tag = image_tag
        result.image_ref = image_ref
        result.metadata |= {
            "local_image_tag": image_tag,
            "registry_image_ref": image_ref if image_ref != image_tag else None,
        }
        return result

    def run_tests(self, deployment):
        workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "tests.log"
        test_command = shlex.split(deployment.build.test_command or "")
        if not test_command:
            raise WorkerExecutionError("tests", "Configured test command is empty")

        result = self._run_command(
            "tests",
            ["docker", "run", "--rm", deployment.build.image_tag, *test_command],
            log_path=log_path,
        )
        result.workspace_path = str(workspace_dir)
        return result

    def tag_image(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "tag.log"
        local_image_tag = deployment.build.image_tag
        registry_image_ref = deployment.build.image_ref
        if not local_image_tag:
            raise WorkerExecutionError("image.tag", "Local image tag is missing before registry tag step")
        if not self.registry_enabled:
            log_path.write_text("Registry push disabled. Tag step skipped.\n", encoding="utf-8")
            return ExecutionResult(
                "Image tag skipped because registry push is disabled",
                metadata={"executor": self.deploy_target, "skipped": True, "registry_enabled": False},
                log_path=str(log_path),
                image_tag=local_image_tag,
                image_ref=registry_image_ref or local_image_tag,
            )
        if not registry_image_ref:
            raise WorkerExecutionError("image.tag", "Registry image reference is missing before push step")

        result = self._run_command(
            "image.tag",
            ["docker", "tag", local_image_tag, registry_image_ref],
            log_path=log_path,
        )
        result.image_tag = local_image_tag
        result.image_ref = registry_image_ref
        return result

    def push_image(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "push.log"
        if not self.registry_enabled:
            log_path.write_text("Push skipped because registry push is disabled.\n", encoding="utf-8")
            return ExecutionResult(
                "Image push skipped because registry push is disabled",
                metadata={"executor": self.deploy_target, "skipped": True, "registry_enabled": False},
                log_path=str(log_path),
                image_tag=deployment.build.image_tag,
                image_ref=deployment.build.image_ref or deployment.build.image_tag,
            )

        registry_image_ref = deployment.build.image_ref
        if not registry_image_ref:
            raise WorkerExecutionError("image.push", "Registry image reference is missing before push step")

        login_metadata = {}
        if self.registry_username and self.registry_password:
            login_result = self._run_command(
                "image.login",
                ["docker", "login", self.registry_url, "--username", self.registry_username, "--password-stdin"],
                log_path=log_path,
                stdin_input=self.registry_password,
            )
            login_metadata = {"login_summary": login_result.message}

        push_result = self._run_command(
            "image.push",
            ["docker", "push", registry_image_ref],
            log_path=log_path,
        )
        push_result.image_tag = deployment.build.image_tag
        push_result.image_ref = registry_image_ref
        push_result.metadata |= login_metadata
        return push_result

    def preflight_deploy(self, deployment):
        return PreflightResult(
            status="skipped",
            summary="Deployment preflight skipped for local-docker executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            deploy_target=self.deploy_target,
        )

    def deploy(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "deploy.log"
        runtime_log_path = logs_dir / "runtime.log"
        container_name = self._container_name(deployment)
        host_port = self.port_allocator()
        published_port = f"{self.deploy_host}:{host_port}:{deployment.project.port}"
        service_url = f"http://{self.deploy_host}:{host_port}"
        healthcheck_url = f"{service_url}{deployment.project.healthcheck_path}"
        env_args, secret_values = self._env_args_with_redaction(deployment)

        self._remove_container_if_exists(container_name)
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
        if workspace_path:
            workspace_dir = Path(workspace_path)
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
                workspace_removed = True
                removed_paths.append(str(workspace_dir))

        log_path = getattr(deployment.build, "log_path", None)
        if log_path:
            log_file = Path(log_path)
            if log_file.exists():
                log_file.unlink()
                log_removed = True
                removed_paths.append(str(log_file))

        return {
            "workspace_removed": workspace_removed,
            "log_removed": log_removed,
            "removed_paths": removed_paths,
        }

    def _wait_for_healthcheck(self, healthcheck_url, log_path, *, redacted_values=()):
        deadline = time.monotonic() + self.healthcheck_timeout
        attempts = 0
        last_error = None
        last_summary = None
        last_heartbeat = time.monotonic()

        while time.monotonic() < deadline:
            last_heartbeat = self._heartbeat_if_due(last_heartbeat)
            attempts += 1
            try:
                probe_result = self.health_probe(healthcheck_url)
            except Exception as exc:
                last_error = redact_text(str(exc), secret_values=redacted_values)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"Healthcheck attempt {attempts} failed: {last_error}\n")
                self.sleep_fn(self.healthcheck_interval)
                continue

            summary = redact_text(probe_result["summary"], secret_values=redacted_values)
            metadata = {
                "healthcheck_attempts": attempts,
                "healthcheck_status_code": probe_result["status_code"],
                "healthcheck_summary": summary,
            }
            if 200 <= probe_result["status_code"] < 400:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"Healthcheck attempt {attempts} succeeded: {probe_result['status_code']} {summary}\n"
                    )
                return metadata

            last_error = f"HTTP {probe_result['status_code']}"
            last_summary = summary
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"Healthcheck attempt {attempts} failed: {probe_result['status_code']} {summary}\n"
                )
            self.sleep_fn(self.healthcheck_interval)

        raise WorkerExecutionError(
            "deploy.healthcheck",
            f"Healthcheck did not succeed within {self.healthcheck_timeout} seconds",
            metadata={
                "healthcheck_attempts": attempts,
                "healthcheck_last_error": last_error,
                "healthcheck_last_summary": last_summary,
                "healthcheck_url": healthcheck_url,
            },
            log_path=str(log_path),
        )

    def _prepare_workspace(self, deployment):
        workspace_dir = self.workspace_root / f"project-{deployment.project_id}" / f"deployment-{deployment.id}"
        repo_dir = workspace_dir / "repo"
        logs_dir = workspace_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir, repo_dir, logs_dir

    @staticmethod
    def _container_name(deployment):
        safe_project_name = "".join(char if char.isalnum() or char == "-" else "-" for char in deployment.project.name.lower())
        return f"paas-{safe_project_name}-{deployment.id}"

    @staticmethod
    def _allocate_host_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _default_health_probe(url):
        try:
            with urlopen(url, timeout=5) as response:  # nosec B310
                body = response.read(512).decode("utf-8", errors="replace")
                return {
                    "status_code": response.status,
                    "summary": body.strip()[:200] or f"HTTP {response.status}",
                }
        except HTTPError as exc:
            body = exc.read(512).decode("utf-8", errors="replace")
            return {
                "status_code": exc.code,
                "summary": body.strip()[:200] or f"HTTP {exc.code}",
            }

    @staticmethod
    def _sanitize_image_component(value):
        sanitized = "".join(char.lower() if char.isalnum() or char in {"-", "_", "."} else "-" for char in value)
        return sanitized.strip("-") or "app"

    def _image_name(self, deployment):
        return self._sanitize_image_component(deployment.build.image_name or deployment.project.name)

    def _tag_suffix(self, deployment):
        raw = deployment.build.image_tag or deployment.build.commit_sha[:12] or str(deployment.id)
        if ":" in raw:
            raw = raw.rsplit(":", 1)[-1]
        return self._sanitize_image_component(raw)

    def _registry_image_ref(self, image_name, tag_suffix):
        if not self.registry_enabled:
            return None
        if not self.registry_url:
            raise WorkerExecutionError("image.tag", "Registry is enabled but CONTROL_PLANE_REGISTRY_URL is not configured")
        namespace = f"{self.registry_namespace}/" if self.registry_namespace else ""
        return f"{self.registry_url}/{namespace}{image_name}:{tag_suffix}"

    def _capture_container_logs(self, deployment, container_name, log_path, *, step, missing_ok):
        result = self._execute_command(["docker", "logs", container_name], allow_heartbeat=False)
        combined_output = redact_text(
            (result.stdout or "") + (result.stderr or ""),
            secret_values=secret_values_from_env_vars(deployment.project.env_vars if deployment.project else []),
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
        args = []
        redacted_values = []
        for item in deployment.project.env_vars or []:
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

    def _run_command(self, step, args, *, log_path, stdin_input=None, env=None, redacted_values=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total_attempts = 1 + self.retry_count if step in self.retryable_steps else 1
        last_error = None
        redacted_values = tuple(value for value in (redacted_values or []) if value)

        for attempt in range(1, total_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                completed = self._execute_command(args, stdin_input=stdin_input, env=env)
            except subprocess.TimeoutExpired as exc:
                output = self._sanitize_text((exc.stdout or "") + (exc.stderr or ""), redacted_values=redacted_values)
                metadata = self._build_command_metadata(
                    args=args,
                    output=output,
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    timed_out=True,
                    redacted_values=redacted_values,
                )
                self._write_log(log_path, args, output, metadata=metadata, append=attempt > 1, redacted_values=redacted_values)
                last_error = WorkerExecutionError(
                    step,
                    f"Command timed out after {self.command_timeout} seconds",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            except OSError as exc:
                output = self._sanitize_text(str(exc), redacted_values=redacted_values)
                metadata = self._build_command_metadata(
                    args=args,
                    output=output,
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    redacted_values=redacted_values,
                )
                self._write_log(log_path, args, output, metadata=metadata, append=attempt > 1, redacted_values=redacted_values)
                last_error = WorkerExecutionError(
                    step,
                    f"Command execution failed: {exc}",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            else:
                combined_output = self._sanitize_text(
                    (completed.stdout or "") + (completed.stderr or ""),
                    redacted_values=redacted_values,
                )
                metadata = self._build_command_metadata(
                    args=args,
                    output=combined_output,
                    started_at=started_at,
                    returncode=completed.returncode,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    redacted_values=redacted_values,
                )
                self._write_log(
                    log_path,
                    args,
                    combined_output,
                    metadata=metadata,
                    append=attempt > 1,
                    redacted_values=redacted_values,
                )
                if completed.returncode == 0:
                    message = self._summarize_output(combined_output) or f"{step} completed successfully"
                    return ExecutionResult(message, metadata=metadata, log_path=str(log_path))

                last_error = WorkerExecutionError(
                    step,
                    metadata["summary"] or f"Command failed with exit code {completed.returncode}",
                    metadata=metadata,
                    log_path=str(log_path),
                )

            if attempt < total_attempts:
                self.sleep_fn(min(attempt, 3))

        raise last_error

    def _execute_command(self, args, *, allow_heartbeat=True, stdin_input=None, env=None):
        heartbeat_cb = self.heartbeat if allow_heartbeat else None
        if self.runner is not None:
            try:
                return self.runner(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout,
                    check=False,
                    input=stdin_input,
                    env=env,
                    heartbeat_cb=heartbeat_cb,
                    heartbeat_interval_seconds=self.heartbeat_interval,
                )
            except TypeError:
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                        env=env,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                        heartbeat_cb=heartbeat_cb,
                        heartbeat_interval_seconds=self.heartbeat_interval,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        heartbeat_cb=heartbeat_cb,
                        heartbeat_interval_seconds=self.heartbeat_interval,
                    )
                except TypeError:
                    pass
                return self.runner(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout,
                    check=False,
                )

        if stdin_input is not None:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
                input=stdin_input,
                env=env,
            )

        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        deadline = time.monotonic() + self.command_timeout
        last_heartbeat = time.monotonic()

        while True:
            returncode = process.poll()
            if returncode is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)

            now = time.monotonic()
            if now >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(args, self.command_timeout, output=stdout, stderr=stderr)

            try:
                last_heartbeat = self._heartbeat_if_due(last_heartbeat, heartbeat=heartbeat_cb)
            except Exception:
                process.kill()
                process.wait(timeout=5)
                raise

            self.sleep_fn(min(0.5, max(0.05, deadline - now)))

    def _heartbeat_if_due(self, last_heartbeat, *, heartbeat=None):
        heartbeat = self.heartbeat if heartbeat is None else heartbeat
        if heartbeat is None:
            return last_heartbeat
        now = time.monotonic()
        if now - last_heartbeat >= self.heartbeat_interval:
            heartbeat()
            return now
        return last_heartbeat

    @staticmethod
    def _summarize_output(output):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return None
        if len(lines) == 1:
            return lines[0][:500]
        return f"{lines[-2][:200]} | {lines[-1][:200]}"

    @staticmethod
    def _tail_lines(output, *, limit=10, line_width=240):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return [line[:line_width] for line in lines[-limit:]]

    def _build_command_metadata(
        self,
        *,
        args,
        output,
        started_at,
        returncode,
        attempt,
        total_attempts,
        timed_out=False,
        redacted_values=None,
    ):
        finished_at = datetime.now(timezone.utc)
        return {
            "command": self._sanitize_args(args, redacted_values=redacted_values),
            "returncode": returncode,
            "attempt": attempt,
            "total_attempts": total_attempts,
            "timed_out": timed_out,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
            "summary": self._summarize_output(output),
            "output_tail": self._tail_lines(output),
        }

    def _write_log(self, log_path, args, output, *, metadata=None, append=False, redacted_values=None):
        sanitized_args = self._sanitize_args(args, redacted_values=redacted_values)
        rendered = ["Command:", " ".join(shlex.quote(part) for part in sanitized_args)]
        if metadata:
            rendered.extend(
                [
                    "",
                    "Metadata:",
                    f"attempt={metadata.get('attempt')}/{metadata.get('total_attempts')}",
                    f"returncode={metadata.get('returncode')}",
                    f"timed_out={metadata.get('timed_out')}",
                    f"duration_seconds={metadata.get('duration_seconds')}",
                ]
            )
        rendered.extend(["", "Output:", output])
        payload = "\n".join(rendered) + "\n"
        mode = "a" if append else "w"
        with log_path.open(mode, encoding="utf-8") as handle:
            if append:
                handle.write("\n==== retry ====\n")
            handle.write(payload)

    @staticmethod
    def _sanitize_text(text, *, redacted_values=None):
        sanitized = text
        for value in redacted_values or ():
            if value:
                sanitized = sanitized.replace(value, "***")
        return sanitized

    def _sanitize_args(self, args, *, redacted_values=None):
        return [self._sanitize_text(str(part), redacted_values=redacted_values) for part in args]

    @staticmethod
    def _event(event_type, status, message, *, step=None, level="info", metadata=None):
        return {
            "event_type": event_type,
            "status": status,
            "message": message,
            "step": step,
            "level": level,
            "metadata_json": redact_sensitive_data(metadata),
        }

    def _git_clone_environment(self, deployment):
        git_auth_type = (getattr(deployment.project, "git_auth_type", None) or "none").strip().lower()
        if git_auth_type == "none":
            return None, ()
        if git_auth_type != "token":
            raise WorkerExecutionError("repository.clone", f"Unsupported git auth type '{git_auth_type}'")

        secret_ref = (getattr(deployment.project, "git_secret_ref", None) or "").strip()
        if not secret_ref:
            raise WorkerExecutionError("repository.clone", "git_secret_ref is required when git_auth_type is 'token'")

        token_env_name = f"CONTROL_PLANE_GIT_TOKEN_{secret_ref}"
        token = os.getenv(token_env_name)
        if not token:
            raise WorkerExecutionError(
                "repository.clone",
                f"Git token environment variable '{token_env_name}' is not set",
            )

        repo_url = deployment.project.repo_url or ""
        if not repo_url.startswith("https://github.com/"):
            raise WorkerExecutionError(
                "repository.clone",
                "Token-based git auth currently supports only https://github.com/ repository URLs",
            )

        auth_header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": auth_header,
            }
        )
        return env, (token, auth_header)


class KubernetesExecutor(LocalDockerExecutor):
    deploy_target = "kubernetes"
    contract = ExecutorContract(
        name="kubernetes",
        deploy_target="kubernetes",
        runtime="kubernetes",
        healthcheck_strategy="service-port-forward",
        requires_registry_push=True,
        supports_runtime_logs=True,
        supports_runtime_reconciliation=True,
        managed_resources=("docker-image", "kubernetes-deployment", "kubernetes-service"),
        required_config=(
            "CONTROL_PLANE_REGISTRY_ENABLED=true",
            "CONTROL_PLANE_REGISTRY_URL",
            "CONTROL_PLANE_REGISTRY_NAMESPACE",
            "CONTROL_PLANE_KUBECONFIG",
        ),
        optional_config=(
            "CONTROL_PLANE_K8S_NAMESPACE",
            "CONTROL_PLANE_K8S_IMAGE_PULL_SECRET",
            "CONTROL_PLANE_REGISTRY_USERNAME",
            "CONTROL_PLANE_REGISTRY_PASSWORD",
            "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
            "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
            "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
        ),
    )

    def __init__(
        self,
        workspace_root,
        command_timeout,
        runner=None,
        retry_count=1,
        sleep_fn=None,
        port_allocator=None,
        health_probe=None,
        deploy_host="127.0.0.1",
        healthcheck_timeout=30,
        healthcheck_interval=1,
        heartbeat_interval=30,
        registry_enabled=False,
        registry_url=None,
        registry_namespace=None,
        registry_username=None,
        registry_password=None,
        popen_factory=None,
        kubeconfig=None,
        namespace="default",
        kubectl_bin="kubectl",
        image_pull_secret=None,
    ):
        super().__init__(
            workspace_root=workspace_root,
            command_timeout=command_timeout,
            runner=runner,
            retry_count=retry_count,
            sleep_fn=sleep_fn,
            port_allocator=port_allocator,
            health_probe=health_probe,
            deploy_host=deploy_host,
            healthcheck_timeout=healthcheck_timeout,
            healthcheck_interval=healthcheck_interval,
            heartbeat_interval=heartbeat_interval,
            registry_enabled=registry_enabled,
            registry_url=registry_url,
            registry_namespace=registry_namespace,
            registry_username=registry_username,
            registry_password=registry_password,
            popen_factory=popen_factory,
        )
        self.kubeconfig = kubeconfig
        self.namespace = namespace or "default"
        self.kubectl_bin = kubectl_bin
        self.image_pull_secret = (image_pull_secret or "").strip() or None

    def deploy(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        manifest_path = logs_dir / "kubernetes-manifest.json"
        apply_log_path = logs_dir / "kubernetes-apply.log"
        rollout_log_path = logs_dir / "kubernetes-rollout.log"
        port_forward_log_path = logs_dir / "kubernetes-port-forward.log"
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        service_url = self._service_url(service_name, deployment.project.port)
        healthcheck_url = f"{service_url}{deployment.project.healthcheck_path}"
        manifest = self._manifest(deployment, deployment_name=deployment_name, service_name=service_name)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        events = [
            self._event(
                "kubernetes.manifest_apply_started",
                "deploying",
                "Applying Kubernetes Deployment and Service manifests",
                step="deploy.kubernetes.apply",
                metadata={"manifest_path": str(manifest_path), "namespace": self.namespace},
            )
        ]

        try:
            apply_result = self._run_kubectl_with_events(
                "deploy.kubernetes.apply",
                self._kubectl_args("apply", "-f", str(manifest_path)),
                log_path=apply_log_path,
                success_event=self._event(
                    "kubernetes.manifest_apply_succeeded",
                    "deploying",
                    "Kubernetes manifests applied successfully",
                    step="deploy.kubernetes.apply",
                    metadata={"manifest_path": str(manifest_path), "namespace": self.namespace},
                ),
                failure_event_type="kubernetes.manifest_apply_failed",
                failure_step="deploy.kubernetes.apply",
                failure_metadata={"manifest_path": str(manifest_path), "namespace": self.namespace},
                existing_events=events,
            )
        except WorkerExecutionError as exc:
            diagnostics = self._collect_apply_diagnostics(logs_dir=logs_dir)
            merged_metadata = exc.metadata | diagnostics
            merged_events = list(exc.events)
            if merged_events:
                merged_event = dict(merged_events[-1])
                merged_event["metadata_json"] = (merged_event.get("metadata_json") or {}) | diagnostics
                merged_events[-1] = merged_event
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata=merged_metadata,
                log_path=exc.log_path,
                events=merged_events,
            ) from exc
        events.extend(
            [
                apply_result.events[-1],
                self._event(
                    "kubernetes.service_configured",
                    "deploying",
                    f"Kubernetes Service '{service_name}' configured",
                    step="deploy.kubernetes.service",
                    metadata={
                        "service_name": service_name,
                        "namespace": self.namespace,
                        "service_url": service_url,
                        "port": deployment.project.port,
                    },
                ),
                self._event(
                    "kubernetes.rollout_started",
                    "deploying",
                    f"Waiting for rollout of Deployment '{deployment_name}'",
                    step="deploy.kubernetes.rollout",
                    metadata={"deployment_name": deployment_name, "namespace": self.namespace},
                ),
            ]
        )

        try:
            rollout_result = self._run_kubectl_with_events(
                "deploy.kubernetes.rollout",
                self._kubectl_args(
                    "rollout",
                    "status",
                    f"deployment/{deployment_name}",
                    "--timeout",
                    f"{self.healthcheck_timeout}s",
                ),
                log_path=rollout_log_path,
                success_event=self._event(
                    "kubernetes.rollout_succeeded",
                    "deploying",
                    "Kubernetes rollout completed successfully",
                    step="deploy.kubernetes.rollout",
                    metadata={"deployment_name": deployment_name, "namespace": self.namespace},
                ),
                failure_event_type="kubernetes.rollout_failed",
                failure_step="deploy.kubernetes.rollout",
                failure_metadata={"deployment_name": deployment_name, "namespace": self.namespace},
                existing_events=events,
            )
        except WorkerExecutionError as exc:
            diagnostics = self._collect_rollout_diagnostics(deployment_name, logs_dir=logs_dir)
            merged_metadata = exc.metadata | diagnostics
            merged_events = list(exc.events)
            if merged_events:
                merged_event = dict(merged_events[-1])
                merged_event["metadata_json"] = (merged_event.get("metadata_json") or {}) | diagnostics
                merged_events[-1] = merged_event
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata=merged_metadata,
                log_path=exc.log_path,
                events=merged_events,
            ) from exc
        events.append(rollout_result.events[-1])
        events.append(
            self._event(
                "kubernetes.healthcheck_started",
                "deploying",
                f"Waiting for healthcheck on Service '{service_name}'",
                step="deploy.kubernetes.healthcheck",
                metadata={"service_name": service_name, "namespace": self.namespace, "healthcheck_url": healthcheck_url},
            )
        )

        try:
            health_metadata = self._port_forward_healthcheck(
                deployment,
                service_name=service_name,
                log_path=port_forward_log_path,
            )
        except WorkerExecutionError as exc:
            diagnostics = self._collect_healthcheck_diagnostics(
                deployment_name,
                service_name=service_name,
                logs_dir=logs_dir,
            )
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata=exc.metadata
                | diagnostics
                | {
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "namespace": self.namespace,
                    "service_url": service_url,
                    "healthcheck_url": healthcheck_url,
                    "manifest_path": str(manifest_path),
                },
                log_path=exc.log_path,
                events=events
                + [
                    self._event(
                        "kubernetes.healthcheck_failed",
                        "failed",
                        exc.message,
                        step="deploy.kubernetes.healthcheck",
                        level="error",
                        metadata=exc.metadata
                        | diagnostics
                        | {
                            "deployment_name": deployment_name,
                            "service_name": service_name,
                            "namespace": self.namespace,
                            "service_url": service_url,
                            "healthcheck_url": healthcheck_url,
                        },
                    )
                ],
            ) from exc

        events.append(
            self._event(
                "kubernetes.healthcheck_succeeded",
                "deploying",
                "Kubernetes Service passed healthcheck",
                step="deploy.kubernetes.healthcheck",
                metadata=health_metadata
                | {
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "namespace": self.namespace,
                    "service_url": service_url,
                    "healthcheck_url": healthcheck_url,
                },
            )
        )

        return ExecutionResult(
            "Kubernetes Deployment rolled out and passed healthcheck.",
            metadata={
                "executor": self.deploy_target,
                "deployment_name": deployment_name,
                "service_name": service_name,
                "namespace": self.namespace,
                "manifest_path": str(manifest_path),
                "service_url": service_url,
                "healthcheck_url": healthcheck_url,
                "image_ref": deployment.build.image_ref,
                **self._env_source_summary(deployment.project.env_vars),
                **health_metadata,
            },
            events=events,
            log_path=str(rollout_log_path),
            service_url=service_url,
            deploy_target=self.deploy_target,
            healthcheck_url=healthcheck_url,
        )

    def preflight_deploy(self, deployment):
        self._ensure_registry_ready(deployment)
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "kubernetes-preflight.log"
        events = [
            self._event(
                "kubernetes.preflight_started",
                "deploying",
                "Checking Kubernetes referenced resources before apply",
                step="deploy.kubernetes.preflight",
                metadata={"namespace": self.namespace},
            )
        ]

        return self._preflight_referenced_resources(
            deployment,
            log_path=log_path,
            existing_events=events,
        )

    def stop(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "kubernetes-delete.log"
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        start_event = self._event(
            "kubernetes.resources_delete_started",
            "stopped",
            "Deleting Kubernetes Deployment and Service resources",
            step="deploy.kubernetes.delete",
            metadata={"deployment_name": deployment_name, "service_name": service_name, "namespace": self.namespace},
        )
        delete_result = self._run_kubectl_with_events(
            "deploy.kubernetes.delete",
            self._kubectl_args(
                "delete",
                f"deployment/{deployment_name}",
                f"service/{service_name}",
                "--ignore-not-found=true",
                "--wait=false",
            ),
            log_path=log_path,
            success_event=self._event(
                "kubernetes.resources_deleted",
                "stopped",
                "Kubernetes resources deleted successfully",
                step="deploy.kubernetes.delete",
                metadata={"deployment_name": deployment_name, "service_name": service_name, "namespace": self.namespace},
            ),
            failure_event_type="kubernetes.resources_delete_failed",
            failure_step="deploy.kubernetes.delete",
            failure_metadata={"deployment_name": deployment_name, "service_name": service_name, "namespace": self.namespace},
            existing_events=[start_event],
        )
        return ExecutionResult(
            "Kubernetes resources deleted successfully.",
            metadata={
                "executor": self.deploy_target,
                "deployment_name": deployment_name,
                "service_name": service_name,
                "namespace": self.namespace,
                "stopped": True,
            },
            events=[start_event, delete_result.events[-1]],
            log_path=str(log_path),
            deploy_target=self.deploy_target,
        )

    def runtime_resource_status(self, deployment):
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        deployment_exists = self._kubectl_resource_exists("deployment", deployment_name)
        service_exists = self._kubectl_resource_exists("service", service_name)
        return {
            "namespace": self.namespace,
            "deployment_name": deployment_name,
            "service_name": service_name,
            "deployment_exists": deployment_exists,
            "service_exists": service_exists,
        }

    def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile"):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        deployment_name = self._k8s_deployment_name(deployment)
        return self._collect_pod_diagnostics(deployment_name, prefix=prefix, logs_dir=logs_dir)

    def _ensure_registry_ready(self, deployment):
        if not self.registry_enabled:
            raise WorkerExecutionError(
                "deployment.apply",
                "Kubernetes deployments require registry push support to be enabled",
            )
        if deployment.build.registry_push_status != "succeeded":
            raise WorkerExecutionError(
                "deployment.apply",
                "Kubernetes deployments require a successful registry push before deploy",
                metadata={"registry_push_status": deployment.build.registry_push_status},
            )
        if not deployment.build.image_ref or deployment.build.image_ref == deployment.build.image_tag:
            raise WorkerExecutionError(
                "deployment.apply",
                "Kubernetes deployments require a pushed image_ref",
                metadata={"image_ref": deployment.build.image_ref},
            )

    def _run_kubectl_with_events(
        self,
        step,
        args,
        *,
        log_path,
        success_event,
        failure_event_type,
        failure_step,
        failure_metadata,
        existing_events,
    ):
        try:
            result = self._run_command(step, args, log_path=log_path)
        except WorkerExecutionError as exc:
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata=exc.metadata | failure_metadata,
                log_path=exc.log_path,
                events=existing_events
                + [
                    self._event(
                        failure_event_type,
                        "failed",
                        exc.message,
                        step=failure_step,
                        level="error",
                        metadata=exc.metadata | failure_metadata,
                    )
                ],
            ) from exc
        success_event["metadata_json"] = (success_event.get("metadata_json") or {}) | result.metadata
        result.events = existing_events + [success_event]
        return result

    def _preflight_referenced_resources(self, deployment, *, log_path, existing_events):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        references = self._kubernetes_referenced_resources(deployment.project.env_vars)
        if self.image_pull_secret:
            references["image_pull_secret"] = self.image_pull_secret

        commands = [
            self._kubectl_args("get", f"configmap/{name}")
            for name in references["configmaps"]
        ] + [
            self._kubectl_args("get", f"secret/{name}")
            for name in references["secrets"]
        ]
        if references.get("image_pull_secret"):
            commands.append(self._kubectl_args("get", f"secret/{references['image_pull_secret']}"))

        checked_resources = []
        missing_resources = []
        log_lines = []

        for name in references["configmaps"]:
            exists = self._kubectl_resource_exists_for_preflight("configmap", name)
            checked_resources.append(f"configmap/{name}")
            log_lines.append(f"configmap/{name}: {'found' if exists else 'missing'}")
            if not exists:
                missing_resources.append({"kind": "ConfigMap", "name": name})

        for name in references["secrets"]:
            exists = self._kubectl_resource_exists_for_preflight("secret", name)
            checked_resources.append(f"secret/{name}")
            log_lines.append(f"secret/{name}: {'found' if exists else 'missing'}")
            if not exists:
                missing_resources.append({"kind": "Secret", "name": name, "usage": "env_var_ref"})

        if references.get("image_pull_secret"):
            name = references["image_pull_secret"]
            exists = self._kubectl_resource_exists_for_preflight("secret", name)
            checked_resources.append(f"secret/{name}")
            log_lines.append(f"secret/{name}: {'found' if exists else 'missing'}")
            if not exists:
                missing_resources.append({"kind": "Secret", "name": name, "usage": "image_pull_secret"})

        self._write_log(log_path, commands, "\n".join(log_lines) + ("\n" if log_lines else ""))

        metadata = {
            "namespace": self.namespace,
            "preflight_log_path": str(log_path),
            "checked_resources": checked_resources,
            "missing_resources": missing_resources,
            "missing_resource_names": [item["name"] for item in missing_resources],
            "missing_resource_types": sorted({item["kind"] for item in missing_resources}),
            "image_pull_secret": references.get("image_pull_secret"),
            **self._env_source_summary(deployment.project.env_vars),
        }

        if missing_resources:
            message = "Missing Kubernetes referenced resources: " + ", ".join(
                f"{item['kind']}/{item['name']}" for item in missing_resources
            )
            raise WorkerExecutionError(
                "deploy.kubernetes.preflight",
                message,
                metadata=metadata,
                log_path=str(log_path),
                events=existing_events
                + [
                    self._event(
                        "kubernetes.preflight_failed",
                        "failed",
                        message,
                        step="deploy.kubernetes.preflight",
                        level="error",
                        metadata=metadata,
                    )
                ],
            )

        success_event = self._event(
            "kubernetes.preflight_succeeded",
            "deploying",
            "Kubernetes referenced resources are available",
            step="deploy.kubernetes.preflight",
            metadata=metadata,
        )
        return PreflightResult(
            status="succeeded",
            summary="Kubernetes referenced resources are available",
            metadata=metadata,
            events=existing_events + [success_event],
            log_path=str(log_path),
            deploy_target=self.deploy_target,
        )

    def _manifest(self, deployment, *, deployment_name, service_name):
        labels = {
            "app.kubernetes.io/name": self._sanitize_image_component(deployment.project.name),
            "app.kubernetes.io/managed-by": "paas-control-plane",
            "app.kubernetes.io/instance": deployment_name,
        }
        literal_secret_names = [
            item.get("name")
            for item in deployment.project.env_vars or []
            if isinstance(item, dict) and env_var_is_secret(item) and item.get("value") is not None
        ]
        if literal_secret_names:
            raise WorkerExecutionError(
                "deploy.kubernetes.manifest",
                "Kubernetes deployments require secret_key_ref for secret env vars",
                metadata={"secret_env_var_names": sorted(str(name) for name in literal_secret_names if name)},
            )
        env = self._kubernetes_env_vars(deployment.project.env_vars)

        pod_spec = {
            "containers": [
                {
                    "name": "app",
                    "image": deployment.build.image_ref,
                    "ports": [{"containerPort": deployment.project.port}],
                    "env": env,
                }
            ]
        }
        if self.image_pull_secret:
            pod_spec["imagePullSecrets"] = [{"name": self.image_pull_secret}]

        return {
            "apiVersion": "v1",
            "kind": "List",
            "items": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": deployment_name, "namespace": self.namespace, "labels": labels},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": labels},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": pod_spec,
                        },
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": service_name, "namespace": self.namespace, "labels": labels},
                    "spec": {
                        "selector": labels,
                        "ports": [
                            {
                                "name": "http",
                                "port": deployment.project.port,
                                "targetPort": deployment.project.port,
                            }
                        ],
                        "type": "ClusterIP",
                    },
                },
            ],
        }

    def _kubernetes_env_vars(self, env_vars):
        rendered = []
        for item in env_vars or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            value_source = item.get("value_source")
            if value_source is None:
                value_source = "literal" if item.get("value") is not None else None
            if value_source == "literal" and item.get("value") is not None:
                rendered.append({"name": name, "value": str(item["value"])})
                continue
            if value_source == "configmap_key_ref" and item.get("source_name") and item.get("source_key"):
                rendered.append(
                    {
                        "name": name,
                        "valueFrom": {
                            "configMapKeyRef": {
                                "name": str(item["source_name"]),
                                "key": str(item["source_key"]),
                            }
                        },
                    }
                )
                continue
            if value_source == "secret_key_ref" and item.get("source_name") and item.get("source_key"):
                rendered.append(
                    {
                        "name": name,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": str(item["source_name"]),
                                "key": str(item["source_key"]),
                            }
                        },
                    }
                )
                continue
        return rendered

    @staticmethod
    def _kubernetes_referenced_resources(env_vars):
        configmaps = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "configmap_key_ref"
                and item.get("source_name")
            }
        )
        secrets = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "secret_key_ref"
                and item.get("source_name")
            }
        )
        return {"configmaps": configmaps, "secrets": secrets}

    @staticmethod
    def _env_source_summary(env_vars):
        configmap_refs = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and (item.get("value_source") == "configmap_key_ref")
                and item.get("source_name")
            }
        )
        secret_refs = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and (item.get("value_source") == "secret_key_ref")
                and item.get("source_name")
            }
        )
        literal_count = sum(
            1
            for item in env_vars or []
            if isinstance(item, dict) and ((item.get("value_source") == "literal") or (item.get("value_source") is None and item.get("value") is not None))
        )
        return {
            "env_var_count": len([item for item in env_vars or [] if isinstance(item, dict) and item.get("name")]),
            "literal_env_count": literal_count,
            "configmap_refs_used": configmap_refs,
            "secret_refs_used": secret_refs,
        }

    def _port_forward_healthcheck(self, deployment, *, service_name, log_path):
        local_port = self.port_allocator()
        probe_url = f"http://127.0.0.1:{local_port}{deployment.project.healthcheck_path}"
        command = self._kubectl_args(
            "port-forward",
            f"service/{service_name}",
            f"{local_port}:{deployment.project.port}",
            "--address",
            "127.0.0.1",
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8")
        process = self.popen_factory(command, stdout=handle, stderr=subprocess.STDOUT, text=True)
        try:
            self.sleep_fn(0.2)
            metadata = self._wait_for_healthcheck(probe_url, log_path)
            metadata["port_forward_local_port"] = local_port
            metadata["port_forward_log_path"] = str(log_path)
            return metadata
        except WorkerExecutionError as exc:
            raise WorkerExecutionError(
                exc.step,
                exc.message,
                metadata=exc.metadata | {"port_forward_local_port": local_port, "port_forward_log_path": str(log_path)},
                log_path=exc.log_path,
            ) from exc
        finally:
            self._terminate_process(process)
            handle.close()

    def _kubectl_args(self, *parts):
        args = [self.kubectl_bin]
        if self.kubeconfig:
            args.extend(["--kubeconfig", self.kubeconfig])
        args.extend(["--namespace", self.namespace])
        args.extend(parts)
        return args

    def _k8s_deployment_name(self, deployment):
        base = self._sanitize_image_component(deployment.project.name)
        return f"paas-{base}-{deployment.id}"

    def _k8s_service_name(self, deployment):
        return f"{self._k8s_deployment_name(deployment)}-svc"

    def _service_url(self, service_name, port):
        return f"http://{service_name}.{self.namespace}.svc.cluster.local:{port}"

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
        describe_log_paths = []
        logs_log_paths = []
        for pod_name in pod_names:
            describe_log_path = logs_dir / f"kubernetes-{prefix}-describe-{pod_name}.log"
            logs_log_path = logs_dir / f"kubernetes-{prefix}-logs-{pod_name}.log"
            describe_output = self._run_diagnostic_command(
                self._kubectl_args("describe", f"pod/{pod_name}"),
                log_path=describe_log_path,
            )
            logs_output = self._run_diagnostic_command(
                self._kubectl_args("logs", f"pod/{pod_name}", "--tail", "50"),
                log_path=logs_log_path,
            )
            describe_log_paths.append(str(describe_log_path))
            logs_log_paths.append(str(logs_log_path))
            describe_summary = self._summarize_output(describe_output)
            logs_summary = self._summarize_output(logs_output)
            if describe_summary:
                describe_summaries.append(f"{pod_name}: {describe_summary}")
            if logs_summary:
                logs_summaries.append(f"{pod_name}: {logs_summary}")

        return {
            f"{prefix}_pod_names_log_path": str(pod_names_log_path),
            f"{prefix}_pod_names": pod_names,
            f"{prefix}_pod_describe_log_paths": describe_log_paths,
            f"{prefix}_pod_describe_summary": " | ".join(describe_summaries)[:1000] if describe_summaries else None,
            f"{prefix}_pod_logs_log_paths": logs_log_paths,
            f"{prefix}_pod_logs_summary": " | ".join(logs_summaries)[:1000] if logs_summaries else None,
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

    def _kubectl_resource_exists(self, kind, name):
        try:
            result = self._execute_command(
                self._kubectl_args("get", f"{kind}/{name}"),
                allow_heartbeat=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkerExecutionError(
                "reconcile.kubernetes_resource_exists",
                f"Failed to inspect Kubernetes resource '{kind}/{name}': {exc}",
                metadata={"kind": kind, "name": name, "namespace": self.namespace},
            ) from exc
        return result.returncode == 0

    def _kubectl_resource_exists_for_preflight(self, kind, name):
        try:
            result = self._execute_command(
                self._kubectl_args("get", f"{kind}/{name}"),
                allow_heartbeat=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkerExecutionError(
                "deploy.kubernetes.preflight",
                f"Failed to inspect Kubernetes resource '{kind}/{name}': {exc}",
                metadata={"kind": kind, "name": name, "namespace": self.namespace},
            ) from exc
        return result.returncode == 0


def executor_contract_for_name(executor_name):
    normalized = (executor_name or "fake").strip().lower()
    contracts = {
        "fake": FakeDeploymentExecutor.contract_spec(),
        "local-docker": LocalDockerExecutor.contract_spec(),
        "kubernetes": KubernetesExecutor.contract_spec(),
    }
    if normalized not in contracts:
        raise RuntimeError(f"Unsupported executor '{normalized}'")
    return contracts[normalized]


def _build_local_docker_executor():
    return LocalDockerExecutor(
        workspace_root=current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
        command_timeout=current_app.config.get("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", 600),
        retry_count=current_app.config.get("CONTROL_PLANE_COMMAND_RETRY_COUNT", 1),
        registry_enabled=current_app.config.get("CONTROL_PLANE_REGISTRY_ENABLED", False),
        registry_url=current_app.config.get("CONTROL_PLANE_REGISTRY_URL"),
        registry_namespace=current_app.config.get("CONTROL_PLANE_REGISTRY_NAMESPACE"),
        registry_username=current_app.config.get("CONTROL_PLANE_REGISTRY_USERNAME"),
        registry_password=current_app.config.get("CONTROL_PLANE_REGISTRY_PASSWORD"),
        deploy_host=current_app.config.get("CONTROL_PLANE_DEPLOY_HOST", "127.0.0.1"),
        healthcheck_timeout=current_app.config.get("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", 30),
        healthcheck_interval=current_app.config.get("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", 1),
        heartbeat_interval=current_app.config.get("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", 30),
    )


def _build_kubernetes_executor():
    return KubernetesExecutor(
        workspace_root=current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces"),
        command_timeout=current_app.config.get("CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS", 600),
        retry_count=current_app.config.get("CONTROL_PLANE_COMMAND_RETRY_COUNT", 1),
        registry_enabled=current_app.config.get("CONTROL_PLANE_REGISTRY_ENABLED", False),
        registry_url=current_app.config.get("CONTROL_PLANE_REGISTRY_URL"),
        registry_namespace=current_app.config.get("CONTROL_PLANE_REGISTRY_NAMESPACE"),
        registry_username=current_app.config.get("CONTROL_PLANE_REGISTRY_USERNAME"),
        registry_password=current_app.config.get("CONTROL_PLANE_REGISTRY_PASSWORD"),
        deploy_host=current_app.config.get("CONTROL_PLANE_DEPLOY_HOST", "127.0.0.1"),
        healthcheck_timeout=current_app.config.get("CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS", 30),
        healthcheck_interval=current_app.config.get("CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS", 1),
        heartbeat_interval=current_app.config.get("CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS", 30),
        kubeconfig=current_app.config.get("CONTROL_PLANE_KUBECONFIG"),
        namespace=current_app.config.get("CONTROL_PLANE_K8S_NAMESPACE", "default"),
        image_pull_secret=current_app.config.get("CONTROL_PLANE_K8S_IMAGE_PULL_SECRET"),
    )


def create_executor():
    executor_name = current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake").strip().lower()
    if executor_name == "local-docker":
        return _build_local_docker_executor()
    if executor_name == "kubernetes":
        return _build_kubernetes_executor()
    if executor_name == "fake":
        return FakeDeploymentExecutor()

    raise RuntimeError(f"Unsupported executor '{executor_name}'")


def create_executor_for_deployment(deployment):
    executor_name = (deployment.deploy_target or current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake")).strip().lower()
    if executor_name == "local-docker":
        return _build_local_docker_executor()
    if executor_name == "kubernetes":
        return _build_kubernetes_executor()
    if executor_name == "fake":
        return FakeDeploymentExecutor()

    raise RuntimeError(f"Unsupported executor '{executor_name}'")
