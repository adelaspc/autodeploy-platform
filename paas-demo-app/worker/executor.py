from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from flask import current_app


@dataclass
class ExecutionResult:
    message: str
    metadata: dict = field(default_factory=dict)
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


class WorkerExecutionError(Exception):
    def __init__(self, step, message, *, metadata=None, log_path=None):
        super().__init__(message)
        self.step = step
        self.message = message
        self.metadata = metadata or {}
        self.log_path = log_path


class DeploymentExecutor:
    deploy_target = "unknown"

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

    def deploy(self, deployment):
        raise NotImplementedError

    def stop(self, deployment):
        raise NotImplementedError

    def container_exists(self, deployment):
        return False

    def cleanup_workspace(self, deployment):
        return {}


class FakeDeploymentExecutor(DeploymentExecutor):
    deploy_target = "fake"

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
    ):
        self.workspace_root = Path(workspace_root)
        self.command_timeout = command_timeout
        self.runner = runner
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

        result = self._run_command(
            "repository.clone",
            [
                "git",
                "clone",
                "--branch",
                deployment.project.branch,
                "--single-branch",
                deployment.project.repo_url,
                str(repo_dir),
            ],
            log_path=log_path,
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
                image_ref,
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

    def deploy(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "deploy.log"
        runtime_log_path = logs_dir / "runtime.log"
        container_name = self._container_name(deployment)
        host_port = self.port_allocator()
        published_port = f"{self.deploy_host}:{host_port}:{deployment.project.port}"
        service_url = f"http://{self.deploy_host}:{host_port}"
        healthcheck_url = f"{service_url}{deployment.project.healthcheck_path}"

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
                *self._env_args(deployment),
                deployment.build.image_tag,
            ],
            log_path=log_path,
        )
        container_id = (run_result.metadata.get("output_tail") or [run_result.message])[-1]

        try:
            health_metadata = self._wait_for_healthcheck(healthcheck_url, log_path)
        except WorkerExecutionError as exc:
            runtime_metadata = self._capture_container_logs(
                container_name,
                runtime_log_path,
                step="deploy.container_logs",
                missing_ok=True,
            )
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

    def _wait_for_healthcheck(self, healthcheck_url, log_path):
        deadline = time.monotonic() + self.healthcheck_timeout
        attempts = 0
        last_error = None
        last_heartbeat = time.monotonic()

        while time.monotonic() < deadline:
            last_heartbeat = self._heartbeat_if_due(last_heartbeat)
            attempts += 1
            try:
                probe_result = self.health_probe(healthcheck_url)
            except Exception as exc:
                last_error = str(exc)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"Healthcheck attempt {attempts} failed: {last_error}\n")
                self.sleep_fn(self.healthcheck_interval)
                continue

            metadata = {
                "healthcheck_attempts": attempts,
                "healthcheck_status_code": probe_result["status_code"],
                "healthcheck_summary": probe_result["summary"],
            }
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"Healthcheck attempt {attempts} succeeded: {probe_result['status_code']} {probe_result['summary']}\n"
                )
            return metadata

        raise WorkerExecutionError(
            "deploy.healthcheck",
            f"Healthcheck did not succeed within {self.healthcheck_timeout} seconds",
            metadata={
                "healthcheck_attempts": attempts,
                "healthcheck_last_error": last_error,
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
        with urlopen(url, timeout=5) as response:
            body = response.read(512).decode("utf-8", errors="replace")
            return {
                "status_code": response.status,
                "summary": body.strip()[:200] or f"HTTP {response.status}",
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

    def _capture_container_logs(self, container_name, log_path, *, step, missing_ok):
        result = self._execute_command(["docker", "logs", container_name], allow_heartbeat=False)
        combined_output = (result.stdout or "") + (result.stderr or "")
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
    def _env_args(deployment):
        args = []
        for item in deployment.project.env_vars or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            args.extend(["--env", f"{name}={value}"])
        return args

    def _run_command(self, step, args, *, log_path, stdin_input=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total_attempts = 1 + self.retry_count if step in self.retryable_steps else 1
        last_error = None

        for attempt in range(1, total_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                completed = self._execute_command(args, stdin_input=stdin_input)
            except subprocess.TimeoutExpired as exc:
                output = (exc.stdout or "") + (exc.stderr or "")
                metadata = self._build_command_metadata(
                    args=args,
                    output=output,
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    timed_out=True,
                )
                self._write_log(log_path, args, output, metadata=metadata, append=attempt > 1)
                last_error = WorkerExecutionError(
                    step,
                    f"Command timed out after {self.command_timeout} seconds",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            except OSError as exc:
                metadata = self._build_command_metadata(
                    args=args,
                    output=str(exc),
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                )
                self._write_log(log_path, args, str(exc), metadata=metadata, append=attempt > 1)
                last_error = WorkerExecutionError(
                    step,
                    f"Command execution failed: {exc}",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            else:
                combined_output = (completed.stdout or "") + (completed.stderr or "")
                metadata = self._build_command_metadata(
                    args=args,
                    output=combined_output,
                    started_at=started_at,
                    returncode=completed.returncode,
                    attempt=attempt,
                    total_attempts=total_attempts,
                )
                self._write_log(log_path, args, combined_output, metadata=metadata, append=attempt > 1)
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

    def _execute_command(self, args, *, allow_heartbeat=True, stdin_input=None):
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
                    )
                except TypeError:
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
            )

        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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

    def _build_command_metadata(self, *, args, output, started_at, returncode, attempt, total_attempts, timed_out=False):
        finished_at = datetime.now(timezone.utc)
        return {
            "command": args,
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

    @staticmethod
    def _write_log(log_path, args, output, *, metadata=None, append=False):
        rendered = ["Command:", " ".join(shlex.quote(part) for part in args)]
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


def create_executor():
    executor_name = current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake").strip().lower()
    if executor_name == "local-docker":
        return LocalDockerExecutor(
            workspace_root=current_app.config["CONTROL_PLANE_WORKSPACE_ROOT"],
            command_timeout=current_app.config["CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS"],
            retry_count=current_app.config["CONTROL_PLANE_COMMAND_RETRY_COUNT"],
            registry_enabled=current_app.config["CONTROL_PLANE_REGISTRY_ENABLED"],
            registry_url=current_app.config["CONTROL_PLANE_REGISTRY_URL"],
            registry_namespace=current_app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"],
            registry_username=current_app.config["CONTROL_PLANE_REGISTRY_USERNAME"],
            registry_password=current_app.config["CONTROL_PLANE_REGISTRY_PASSWORD"],
            deploy_host=current_app.config["CONTROL_PLANE_DEPLOY_HOST"],
            healthcheck_timeout=current_app.config["CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS"],
            healthcheck_interval=current_app.config["CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS"],
            heartbeat_interval=current_app.config["CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS"],
        )
    if executor_name == "fake":
        return FakeDeploymentExecutor()

    raise RuntimeError(f"Unsupported executor '{executor_name}'")


def create_executor_for_deployment(deployment):
    executor_name = (deployment.deploy_target or current_app.config.get("CONTROL_PLANE_EXECUTOR", "fake")).strip().lower()
    if executor_name == "local-docker":
        return LocalDockerExecutor(
            workspace_root=current_app.config["CONTROL_PLANE_WORKSPACE_ROOT"],
            command_timeout=current_app.config["CONTROL_PLANE_COMMAND_TIMEOUT_SECONDS"],
            retry_count=current_app.config["CONTROL_PLANE_COMMAND_RETRY_COUNT"],
            registry_enabled=current_app.config["CONTROL_PLANE_REGISTRY_ENABLED"],
            registry_url=current_app.config["CONTROL_PLANE_REGISTRY_URL"],
            registry_namespace=current_app.config["CONTROL_PLANE_REGISTRY_NAMESPACE"],
            registry_username=current_app.config["CONTROL_PLANE_REGISTRY_USERNAME"],
            registry_password=current_app.config["CONTROL_PLANE_REGISTRY_PASSWORD"],
            deploy_host=current_app.config["CONTROL_PLANE_DEPLOY_HOST"],
            healthcheck_timeout=current_app.config["CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS"],
            healthcheck_interval=current_app.config["CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS"],
            heartbeat_interval=current_app.config["CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS"],
        )
    if executor_name == "fake":
        return FakeDeploymentExecutor()

    raise RuntimeError(f"Unsupported executor '{executor_name}'")
