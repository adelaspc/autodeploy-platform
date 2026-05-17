import subprocess
from pathlib import Path
import pytest

from worker.executor import (
    KubernetesExecutor,
    WorkerExecutionError,
    create_executor,
    executor_contract_for_name,
)
from worker.service import process_next_pending_deployment

from tests.test_projects import create_project


class DummyPopen:
    def __init__(self, args, stdout=None, stderr=None, text=None):
        self.args = args
        self.stdout = stdout
        self.stderr = stderr
        self.text = text
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def kill(self):
        self._returncode = -9


def create_pending_deployment(client, *, name="k8s-app", test_command=None):
    project_response = create_project(
        client,
        name=name,
        repo_url="https://github.com/example/k8s-app",
        trigger="manual",
    )
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "registry": "docker.io/example",
            "image_name": name,
            "image_tag": "abc123def456",
            "status": "pending",
            "build_status": "pending",
            "test_command": test_command,
        },
    )
    return project_id, deployment_response.get_json()


def test_kubernetes_manifest_generation_uses_registry_image_and_service():
    executor = KubernetesExecutor(
        workspace_root="/tmp/test-k8s",
        command_timeout=30,
        registry_enabled=True,
        namespace="apps",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 7,
            "project_id": 3,
            "build": type("BuildStub", (), {"image_ref": "docker.io/example/demo:abc123"})(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "Demo App",
                    "port": 5000,
                    "env_vars": [{"name": "APP_ENV", "value": "production"}],
                },
            )(),
        },
    )()

    manifest = executor._manifest(
        deployment,
        deployment_name="paas-demo-app-7",
        service_name="paas-demo-app-7-svc",
    )

    assert manifest["kind"] == "List"
    deployment_manifest, service_manifest = manifest["items"]
    assert deployment_manifest["kind"] == "Deployment"
    assert deployment_manifest["metadata"]["namespace"] == "apps"
    assert deployment_manifest["spec"]["template"]["spec"]["containers"][0]["image"] == "docker.io/example/demo:abc123"
    assert deployment_manifest["spec"]["template"]["spec"]["containers"][0]["env"] == [
        {"name": "APP_ENV", "value": "production"}
    ]
    assert "imagePullSecrets" not in deployment_manifest["spec"]["template"]["spec"]
    assert service_manifest["kind"] == "Service"
    assert service_manifest["spec"]["ports"][0]["port"] == 5000


def test_kubernetes_manifest_generation_includes_image_pull_secret_when_configured():
    executor = KubernetesExecutor(
        workspace_root="/tmp/test-k8s",
        command_timeout=30,
        registry_enabled=True,
        namespace="apps",
        image_pull_secret="dockerhub-pull-secret",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 8,
            "project_id": 3,
            "build": type("BuildStub", (), {"image_ref": "docker.io/example/demo:abc123"})(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "Demo App",
                    "port": 5000,
                    "env_vars": [],
                },
            )(),
        },
    )()

    manifest = executor._manifest(
        deployment,
        deployment_name="paas-demo-app-8",
        service_name="paas-demo-app-8-svc",
    )

    deployment_manifest = manifest["items"][0]
    assert deployment_manifest["spec"]["template"]["spec"]["imagePullSecrets"] == [
        {"name": "dockerhub-pull-secret"}
    ]


def test_kubernetes_manifest_generation_supports_configmap_and_secret_env_refs():
    executor = KubernetesExecutor(
        workspace_root="/tmp/test-k8s",
        command_timeout=30,
        registry_enabled=True,
        namespace="apps",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 9,
            "project_id": 3,
            "build": type("BuildStub", (), {"image_ref": "docker.io/example/demo:abc123"})(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "Demo App",
                    "port": 5000,
                    "env_vars": [
                        {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
                        {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
                    ],
                },
            )(),
        },
    )()

    manifest = executor._manifest(
        deployment,
        deployment_name="paas-demo-app-9",
        service_name="paas-demo-app-9-svc",
    )

    env = manifest["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert env == [
        {
            "name": "APP_ENV",
            "valueFrom": {"configMapKeyRef": {"name": "my-app-config", "key": "app-env"}},
        },
        {
            "name": "DATABASE_URL",
            "valueFrom": {"secretKeyRef": {"name": "my-app-secret", "key": "database-url"}},
        },
    ]


def test_kubernetes_executor_processes_mixed_env_sources_and_records_summary(client, tmp_path):
    _project_id, pending = create_pending_deployment(client, name="k8s-env-success", test_command=None)
    commands = []

    with client.application.app_context():
        from backend.extensions import db
        from backend.models import PlatformDeployment

        deployment = db.session.get(PlatformDeployment, pending["id"])
        deployment.project.env_vars = [
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
            {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
        ]
        db.session.commit()

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:2] in (["docker", "build"], ["docker", "tag"], ["docker", "push"]):
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")
        if args[:3] == ["kubectl", "--namespace", "default"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="kubectl ok\n", stderr="")
        raise AssertionError(f"Unexpected command: {args}")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        port_allocator=lambda: 19090,
        health_probe=lambda _url: {"status_code": 200, "summary": "ok"},
        sleep_fn=lambda _seconds: None,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        popen_factory=DummyPopen,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    deployment = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}").get_json()
    apply_event = next(event for event in deployment["events"] if event["event_type"] == "deployment.apply_succeeded")
    assert apply_event["metadata_json"]["env_var_count"] == 3
    assert apply_event["metadata_json"]["literal_env_count"] == 1
    assert apply_event["metadata_json"]["configmap_refs_used"] == ["my-app-config"]
    assert apply_event["metadata_json"]["secret_refs_used"] == ["my-app-secret"]


def test_create_executor_returns_kubernetes_executor(app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"
        app.config["CONTROL_PLANE_K8S_NAMESPACE"] = "microk8s"
        executor = create_executor()

    assert isinstance(executor, KubernetesExecutor)
    assert executor.kubeconfig == "/tmp/kubeconfig"
    assert executor.namespace == "microk8s"


def test_executor_contract_for_kubernetes_is_explicit():
    contract = executor_contract_for_name("kubernetes")

    assert contract.deploy_target == "kubernetes"
    assert contract.runtime == "kubernetes"
    assert contract.requires_registry_push is True
    assert contract.healthcheck_strategy == "service-port-forward"
    assert contract.managed_resources == (
        "docker-image",
        "kubernetes-deployment",
        "kubernetes-service",
    )
    assert contract.required_config == (
        "CONTROL_PLANE_REGISTRY_ENABLED=true",
        "CONTROL_PLANE_REGISTRY_URL",
        "CONTROL_PLANE_REGISTRY_NAMESPACE",
        "CONTROL_PLANE_KUBECONFIG",
    )


def test_kubernetes_executor_processes_deployment_with_stubbed_kubectl(client, tmp_path):
    _project_id, pending = create_pending_deployment(client, name="k8s-success", test_command=None)
    commands = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="build ok\n", stderr="")
        if args[:2] == ["docker", "tag"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="tag ok\n", stderr="")
        if args[:2] == ["docker", "push"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="push ok\n", stderr="")
        if args[:3] == ["kubectl", "--namespace", "default"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="kubectl ok\n", stderr="")
        raise AssertionError(f"Unexpected command: {args}")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        port_allocator=lambda: 19090,
        health_probe=lambda _url: {"status_code": 200, "summary": "ok"},
        sleep_fn=lambda _seconds: None,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        popen_factory=DummyPopen,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert processed.deploy_target == "kubernetes"
    assert processed.service_url == f"http://paas-k8s-success-{processed.id}-svc.default.svc.cluster.local:5000"

    deployment = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}").get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "kubernetes.preflight_started" in event_types
    assert "kubernetes.preflight_succeeded" in event_types
    assert "kubernetes.manifest_apply_started" in event_types
    assert "kubernetes.manifest_apply_succeeded" in event_types
    assert "kubernetes.service_configured" in event_types
    assert "kubernetes.rollout_started" in event_types
    assert "kubernetes.rollout_succeeded" in event_types
    assert "kubernetes.healthcheck_started" in event_types
    assert "kubernetes.healthcheck_succeeded" in event_types

    assert any(command[:2] == ["docker", "push"] for command in commands)
    assert any("apply" in command for command in commands if command[0] == "kubectl")
    assert any("rollout" in command for command in commands if command[0] == "kubectl")


def test_kubernetes_executor_preflight_fails_when_referenced_resources_are_missing(tmp_path):
    commands = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        if args[:2] == ["kubectl", "--namespace"] or (args and args[0] == "kubectl"):
            if "get" in args and "configmap/my-app-config" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="NotFound\n")
            if "get" in args and "secret/my-app-secret" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="found\n", stderr="")
            if "get" in args and "secret/dockerhub-pull-secret" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="NotFound\n")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="kubectl ok\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        image_pull_secret="dockerhub-pull-secret",
        popen_factory=DummyPopen,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 11,
            "project_id": 3,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "docker.io/example/demo:abc123",
                    "image_tag": "demo:abc123",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [
                        {"name": "APP_ENV", "value_source": "configmap_key_ref", "source_name": "my-app-config", "source_key": "app-env"},
                        {"name": "DATABASE_URL", "value_source": "secret_key_ref", "source_name": "my-app-secret", "source_key": "database-url"},
                    ],
                },
            )(),
        },
    )()

    try:
        executor.preflight_deploy(deployment)
        raise AssertionError("Expected deploy to fail")
    except WorkerExecutionError as exc:
        assert exc.step == "deploy.kubernetes.preflight"
        assert "ConfigMap/my-app-config" in exc.message
        assert "Secret/dockerhub-pull-secret" in exc.message
        failed_event = next(event for event in exc.events if event["event_type"] == "kubernetes.preflight_failed")
        assert failed_event["metadata_json"]["missing_resource_names"] == ["my-app-config", "dockerhub-pull-secret"]
        assert failed_event["metadata_json"]["missing_resource_types"] == ["ConfigMap", "Secret"]
        assert failed_event["metadata_json"]["configmap_refs_used"] == ["my-app-config"]
        assert failed_event["metadata_json"]["secret_refs_used"] == ["my-app-secret"]
        assert failed_event["metadata_json"]["image_pull_secret"] == "dockerhub-pull-secret"

    assert any("configmap/my-app-config" in command for command in commands if command and command[0] == "kubectl")
    assert any("secret/my-app-secret" in command for command in commands if command and command[0] == "kubectl")
    assert any("secret/dockerhub-pull-secret" in command for command in commands if command and command[0] == "kubectl")
    assert not any("apply" in command for command in commands if command and command[0] == "kubectl")


def test_kubernetes_executor_rejects_literal_secret_env_values_in_manifest(tmp_path):
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args=kwargs.get("args", []), returncode=0, stdout="ok\n", stderr=""),
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        popen_factory=DummyPopen,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 12,
            "project_id": 3,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "docker.io/example/demo:abc123",
                    "image_tag": "demo:abc123",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [
                        {"name": "DATABASE_URL", "value": "postgres://secret", "is_secret": True},
                    ],
                },
            )(),
        },
    )()

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor.deploy(deployment)

    assert exc_info.value.step == "deploy.kubernetes.manifest"
    assert "secret_key_ref" in exc_info.value.message


def test_kubernetes_executor_fails_when_rollout_fails(tmp_path):
    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:2] == ["kubectl", "--namespace"] or (args and args[0] == "kubectl"):
            if "rollout" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="rollout timed out\n")
            if "get" in args and "pods" in args:
                if "-l" in args and "-o" in args and "name" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="pod/app-123\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="NAME READY STATUS RESTARTS AGE\napp-123 0/1 ImagePullBackOff 0 15s\n",
                    stderr="",
                )
            if "describe" in args:
                if "pod/app-123" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="Pod Events:\n  Warning  Failed  15s  kubelet  Back-off pulling image\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Events:\n  Warning  Failed  15s  kubelet  Failed to pull image\n",
                    stderr="",
                )
            if "logs" in args and "pod/app-123" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Error: failed to start application\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="apply ok\n", stderr="")
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        popen_factory=DummyPopen,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 2,
            "project_id": 3,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "docker.io/example/demo:abc123",
                    "image_tag": "demo:abc123",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [],
                },
            )(),
        },
    )()

    try:
        executor.deploy(deployment)
        raise AssertionError("Expected deploy to fail")
    except WorkerExecutionError as exc:
        assert exc.step == "deploy.kubernetes.rollout"
        failed_event = next(event for event in exc.events if event["event_type"] == "kubernetes.rollout_failed")
        assert failed_event["metadata_json"]["rollout_pods_summary"] == "NAME READY STATUS RESTARTS AGE | app-123 0/1 ImagePullBackOff 0 15s"
        assert failed_event["metadata_json"]["rollout_describe_summary"] == "Events: | Warning  Failed  15s  kubelet  Failed to pull image"
        assert failed_event["metadata_json"]["rollout_pod_names"] == ["app-123"]
        assert failed_event["metadata_json"]["rollout_pod_describe_summary"] == "app-123: Pod Events: | Warning  Failed  15s  kubelet  Back-off pulling image"
        assert failed_event["metadata_json"]["rollout_pod_logs_summary"] == "app-123: Error: failed to start application"
        assert exc.metadata["rollout_pods_summary"] == "NAME READY STATUS RESTARTS AGE | app-123 0/1 ImagePullBackOff 0 15s"
        assert exc.metadata["rollout_describe_summary"] == "Events: | Warning  Failed  15s  kubelet  Failed to pull image"
        assert exc.metadata["rollout_pod_describe_summary"] == "app-123: Pod Events: | Warning  Failed  15s  kubelet  Back-off pulling image"
        assert exc.metadata["rollout_pod_logs_summary"] == "app-123: Error: failed to start application"


def test_kubernetes_executor_fails_when_manifest_apply_fails(tmp_path):
    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:2] == ["kubectl", "--namespace"] or (args and args[0] == "kubectl"):
            if "apply" in args:
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="admission webhook denied request\n")
            if "get" in args and "pods" in args:
                if "-o" in args and "name" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="pod/app-456\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="No resources found in default namespace.\n",
                    stderr="",
                )
            if "get" in args and "services" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="NAME TYPE CLUSTER-IP EXTERNAL-IP PORT(S) AGE\nkubernetes ClusterIP 10.152.183.1 <none> 443/TCP 1d\n",
                    stderr="",
                )
            if "describe" in args and "pod/app-456" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Pod Events:\n  Warning  FailedCreate  5s  replicaset-controller  Error creating pod\n",
                    stderr="",
                )
            if "logs" in args and "pod/app-456" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="ContainerCreating\n",
                    stderr="",
                )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        popen_factory=DummyPopen,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 9,
            "project_id": 3,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "docker.io/example/demo:abc123",
                    "image_tag": "demo:abc123",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [],
                },
            )(),
        },
    )()

    try:
        executor.deploy(deployment)
        raise AssertionError("Expected deploy to fail")
    except WorkerExecutionError as exc:
        assert exc.step == "deploy.kubernetes.apply"
        failed_event = next(event for event in exc.events if event["event_type"] == "kubernetes.manifest_apply_failed")
        assert failed_event["metadata_json"]["apply_pods_summary"] == "No resources found in default namespace."
        assert failed_event["metadata_json"]["apply_services_summary"] == "NAME TYPE CLUSTER-IP EXTERNAL-IP PORT(S) AGE | kubernetes ClusterIP 10.152.183.1 <none> 443/TCP 1d"
        assert failed_event["metadata_json"]["apply_pod_names"] == ["app-456"]
        assert failed_event["metadata_json"]["apply_pod_describe_summary"] == "app-456: Pod Events: | Warning  FailedCreate  5s  replicaset-controller  Error creating pod"
        assert failed_event["metadata_json"]["apply_pod_logs_summary"] == "app-456: ContainerCreating"
        assert exc.metadata["apply_pods_summary"] == "No resources found in default namespace."
        assert exc.metadata["apply_services_summary"] == "NAME TYPE CLUSTER-IP EXTERNAL-IP PORT(S) AGE | kubernetes ClusterIP 10.152.183.1 <none> 443/TCP 1d"
        assert exc.metadata["apply_pod_describe_summary"] == "app-456: Pod Events: | Warning  FailedCreate  5s  replicaset-controller  Error creating pod"
        assert exc.metadata["apply_pod_logs_summary"] == "app-456: ContainerCreating"


def test_kubernetes_executor_fails_when_healthcheck_fails(tmp_path):
    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:2] == ["kubectl", "--namespace"] or (args and args[0] == "kubectl"):
            if "get" in args and "pods" in args:
                if "-l" in args and "-o" in args and "name" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="pod/app-123\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="NAME READY STATUS RESTARTS AGE\napp-123 1/1 Running 0 30s\n",
                    stderr="",
                )
            if "describe" in args and "deployment/" in args[-1]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Conditions:\n  Available  True\n",
                    stderr="",
                )
            if "describe" in args and "service/" in args[-1]:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Endpoints: <none>\nSession Affinity: None\n",
                    stderr="",
                )
            if "describe" in args and "pod/app-123" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="Pod Conditions:\n  Ready  True\n",
                    stderr="",
                )
            if "logs" in args and "pod/app-123" in args:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="waiting for upstream dependency\n",
                    stderr="",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="kubectl ok\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
        port_allocator=lambda: 19090,
        health_probe=lambda _url: {"status_code": 503, "summary": "not ready"},
        sleep_fn=lambda _seconds: None,
        popen_factory=DummyPopen,
        healthcheck_timeout=1,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 10,
            "project_id": 3,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "docker.io/example/demo:abc123",
                    "image_tag": "demo:abc123",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [],
                },
            )(),
        },
    )()

    try:
        executor.deploy(deployment)
        raise AssertionError("Expected deploy to fail")
    except WorkerExecutionError as exc:
        assert exc.step == "deploy.healthcheck"
        failed_event = next(event for event in exc.events if event["event_type"] == "kubernetes.healthcheck_failed")
        assert failed_event["metadata_json"]["healthcheck_pods_summary"] == "NAME READY STATUS RESTARTS AGE | app-123 1/1 Running 0 30s"
        assert failed_event["metadata_json"]["healthcheck_deployment_summary"] == "Conditions: | Available  True"
        assert failed_event["metadata_json"]["healthcheck_service_summary"] == "Endpoints: <none> | Session Affinity: None"
        assert failed_event["metadata_json"]["healthcheck_pod_names"] == ["app-123"]
        assert failed_event["metadata_json"]["healthcheck_pod_describe_summary"] == "app-123: Pod Conditions: | Ready  True"
        assert failed_event["metadata_json"]["healthcheck_pod_logs_summary"] == "app-123: waiting for upstream dependency"
        assert exc.metadata["healthcheck_pods_summary"] == "NAME READY STATUS RESTARTS AGE | app-123 1/1 Running 0 30s"
        assert exc.metadata["healthcheck_deployment_summary"] == "Conditions: | Available  True"
        assert exc.metadata["healthcheck_service_summary"] == "Endpoints: <none> | Session Affinity: None"
        assert exc.metadata["healthcheck_pod_describe_summary"] == "app-123: Pod Conditions: | Ready  True"
        assert exc.metadata["healthcheck_pod_logs_summary"] == "app-123: waiting for upstream dependency"


def test_kubernetes_stop_deletes_resources(tmp_path):
    commands = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="deleted\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        registry_enabled=True,
        registry_url="docker.io",
        registry_namespace="example",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 5,
            "project_id": 2,
            "project": type("ProjectStub", (), {"name": "demo-app"})(),
        },
    )()

    result = executor.stop(deployment)

    assert result.deploy_target == "kubernetes"
    assert result.events[0]["event_type"] == "kubernetes.resources_delete_started"
    assert result.events[1]["event_type"] == "kubernetes.resources_deleted"
    assert commands[0][:3] == ["kubectl", "--namespace", "default"]
    assert "deployment/paas-demo-app-5" in commands[0]
    assert "service/paas-demo-app-5-svc" in commands[0]
