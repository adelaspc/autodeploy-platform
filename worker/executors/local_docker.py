from __future__ import annotations

import subprocess
import time
from pathlib import Path

from worker.execution.command_runner import CommandExecutionMixin
from worker.execution.contracts import (
    DeploymentExecutor,
    ExecutorContract,
    PreflightResult,
)
from worker.execution.source_build import SourceBuildMixin
from worker.executors.docker_runtime import DockerRuntimeMixin


class LocalDockerExecutor(SourceBuildMixin, DockerRuntimeMixin, CommandExecutionMixin, DeploymentExecutor):
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
    retryable_steps = frozenset({"repository.clone", "image.build", "tests", "image.push", "image.verify"})

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

    def preflight_deploy(self, deployment):
        return PreflightResult(
            status="skipped",
            summary="Deployment preflight skipped for local-docker executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            deploy_target=self.deploy_target,
        )
