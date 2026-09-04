"""Define the result and executor contracts shared by every runtime backend."""

from dataclasses import dataclass, field


@dataclass
class ExecutionResult:
    """Return step output without letting executors persist control-plane state."""
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
    """Runtime boundary shared by fake, local Docker, and Kubernetes modes."""
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

    def verify_image(self, deployment):
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

    def runtime_helm_status(self, deployment):
        return None

    def runtime_pod_diagnostics(self, deployment, *, prefix="reconcile"):
        return {}
