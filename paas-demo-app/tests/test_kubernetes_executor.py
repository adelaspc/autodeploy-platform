import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest

from worker.helm_runner import HelmCommandError, HelmResult
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


class RecordingHelmRunner:
    instances = []

    def __init__(self, *, namespace, helm_binary="helm", chart_path=None, helm_timeout="180s"):
        self.namespace = namespace
        self.helm_binary = helm_binary
        self.chart_path = chart_path
        self.helm_timeout = helm_timeout
        self.upgrade_install_calls = []
        self.status_calls = []
        RecordingHelmRunner.instances.append(self)

    def upgrade_install(self, release, values_file):
        self.upgrade_install_calls.append({"release": release, "values_file": values_file})
        return HelmResult(
            args=[
                self.helm_binary,
                "upgrade",
                "--install",
                release,
                self.chart_path,
                "-f",
                values_file,
            ],
            returncode=0,
            stdout="deployed\n",
            stderr="",
        )

    def uninstall(self, release):
        self.uninstall_call = {"release": release}
        return HelmResult(
            args=[self.helm_binary, "uninstall", release, "--namespace", self.namespace],
            returncode=0,
            stdout="uninstalled\n",
            stderr="",
        )

    def status(self, release):
        self.status_calls.append({"release": release})
        return HelmResult(
            args=[self.helm_binary, "status", release, "--namespace", self.namespace, "--output", "json"],
            returncode=0,
            stdout='{"info": {"status": "deployed"}}\n',
            stderr="",
        )


class FailingHelmRunner(RecordingHelmRunner):
    def upgrade_install(self, release, values_file):
        self.upgrade_install_calls.append({"release": release, "values_file": values_file})
        result = HelmResult(
            args=["helm", "upgrade", "--install", release, self.chart_path, "-f", values_file],
            returncode=1,
            stdout="",
            stderr="helm failed\n",
        )
        raise HelmCommandError(result)


class FailingHelmUninstallRunner(RecordingHelmRunner):
    def uninstall(self, release):
        self.uninstall_call = {"release": release}
        result = HelmResult(
            args=["helm", "uninstall", release, "--namespace", self.namespace],
            returncode=1,
            stdout="",
            stderr="uninstall failed\n",
        )
        raise HelmCommandError(result)


class MissingHelmReleaseRunner(RecordingHelmRunner):
    def status(self, release):
        self.status_calls.append({"release": release})
        result = HelmResult(
            args=["helm", "status", release, "--namespace", self.namespace, "--output", "json"],
            returncode=1,
            stdout="",
            stderr=f"Error: release: not found: {release}\n",
        )
        raise HelmCommandError(result)

    def uninstall(self, release):
        self.uninstall_call = {"release": release}
        result = HelmResult(
            args=["helm", "uninstall", release, "--namespace", self.namespace],
            returncode=1,
            stdout="",
            stderr=f"Error: uninstall: Release not loaded: {release}: release: not found\n",
        )
        raise HelmCommandError(result)


def make_kubernetes_deployment_stub(*, deployment_id=7, project_id=3, name="helm-app", environment="production"):
    return type(
        "DeploymentStub",
        (),
        {
            "id": deployment_id,
            "project_id": project_id,
            "environment": environment,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_ref": "localhost:32000/helm-app:dev",
                    "image_tag": "helm-app:dev",
                    "registry_push_status": "succeeded",
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "id": project_id,
                    "name": name,
                    "port": 3000,
                    "healthcheck_path": "/health",
                    "env_vars": [{"name": "APP_ENV", "value": "production"}],
                    "cpu": None,
                    "memory": None,
                },
            )(),
        },
    )()


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


def test_kubernetes_manifest_generation_includes_ingress_when_enabled():
    executor = KubernetesExecutor(
        workspace_root="/tmp/test-k8s",
        command_timeout=30,
        registry_enabled=True,
        namespace="apps",
        ingress_enabled=True,
        ingress_class_name="nginx",
        ingress_base_domain="127.0.0.1.nip.io",
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=8, name="Demo App")

    manifest = executor._manifest(
        deployment,
        deployment_name="paas-demo-app-8",
        service_name="paas-demo-app-8-svc",
    )

    ingress = manifest["items"][2]
    assert ingress["kind"] == "Ingress"
    assert ingress["spec"]["ingressClassName"] == "nginx"
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == "paas-deployment-h.127.0.0.1.nip.io"
    assert rule["http"]["paths"][0]["backend"]["service"] == {
        "name": "paas-demo-app-8-svc",
        "port": {"number": 3000},
    }


def test_ingress_hostname_encodes_deployment_id_without_digits():
    executor = KubernetesExecutor(
        workspace_root="/tmp/test-k8s",
        command_timeout=30,
        ingress_enabled=True,
        ingress_base_domain="127.0.0.1.nip.io",
    )

    assert executor._ingress_host(1) == "paas-deployment-a.127.0.0.1.nip.io"
    assert executor._ingress_host(26) == "paas-deployment-z.127.0.0.1.nip.io"
    assert executor._ingress_host(27) == "paas-deployment-aa.127.0.0.1.nip.io"


def test_collect_pod_runtime_metadata_returns_first_class_fields(tmp_path):
    pod_payload = {
        "items": [{
            "metadata": {"name": "demo-pod"},
            "spec": {
                "containers": [{"name": "app", "image": "docker.io/example/app:v1"}],
                "imagePullSecrets": [{"name": "dockerhub-pull"}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{
                    "name": "app",
                    "image": "docker.io/example/app:v1",
                    "ready": False,
                    "restartCount": 3,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                }],
            },
        }]
    }

    def runner(args, **_kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(pod_payload), stderr="")

    executor = KubernetesExecutor(workspace_root=tmp_path, command_timeout=30, runner=runner)
    metadata = executor._collect_pod_runtime_metadata("paas-demo-1", prefix="healthcheck", logs_dir=tmp_path)

    assert metadata["healthcheck_pod_phase"] == "Running"
    assert metadata["healthcheck_container_reason"] == "CrashLoopBackOff"
    assert metadata["healthcheck_restart_count"] == 3
    assert metadata["healthcheck_images"] == ["docker.io/example/app:v1"]
    assert metadata["healthcheck_image_pull_secrets"] == ["dockerhub-pull"]


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
        if args[:2] in (["docker", "build"], ["docker", "tag"], ["docker", "push"]) or args[:4] == [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
        ]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")
        if "api-resources" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="ingressclasses.networking.k8s.io\ningresses.networking.k8s.io\n",
                stderr="",
            )
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
        ingress_enabled=True,
        ingress_base_domain="127.0.0.1.nip.io",
        popen_factory=DummyPopen,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.service_url == f"http://paas-deployment-{executor._alphabetic_id(processed.id)}.127.0.0.1.nip.io"
    deployment = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}").get_json()
    apply_event = next(event for event in deployment["events"] if event["event_type"] == "deployment.apply_succeeded")
    assert apply_event["metadata_json"]["env_var_count"] == 3
    assert apply_event["metadata_json"]["literal_env_count"] == 1
    assert apply_event["metadata_json"]["configmap_refs_used"] == ["my-app-config"]
    assert apply_event["metadata_json"]["secret_refs_used"] == ["my-app-secret"]
    summary = client.get(
        f"/api/projects/{processed.project_id}/deployments/{processed.id}/summary"
    ).get_json()
    assert summary["kubernetes_ingress_host"] == (
        f"paas-deployment-{executor._alphabetic_id(processed.id)}.127.0.0.1.nip.io"
    )
    assert summary["internal_service_url"] == (
        f"http://paas-k8s-env-success-{processed.id}-svc.default.svc.cluster.local:5000"
    )


def test_create_executor_returns_kubernetes_executor(app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_REGISTRY_ENABLED"] = True
        app.config["CONTROL_PLANE_KUBECONFIG"] = "/tmp/kubeconfig"
        app.config["CONTROL_PLANE_K8S_NAMESPACE"] = "microk8s"
        app.config["CONTROL_PLANE_K8S_DEPLOYMENT_MODE"] = "helm"
        app.config["CONTROL_PLANE_K8S_HELM_CHART_PATH"] = "/opt/charts/generic-web-app"
        app.config["CONTROL_PLANE_K8S_HELM_BINARY"] = "/usr/local/bin/helm"
        app.config["CONTROL_PLANE_K8S_HELM_TIMEOUT"] = "240s"
        app.config["CONTROL_PLANE_K8S_INGRESS_ENABLED"] = True
        app.config["CONTROL_PLANE_K8S_INGRESS_CLASS_NAME"] = "nginx"
        app.config["CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN"] = "127.0.0.1.nip.io"
        executor = create_executor()

    assert isinstance(executor, KubernetesExecutor)
    assert executor.kubeconfig == "/tmp/kubeconfig"
    assert executor.namespace == "microk8s"
    assert executor.deployment_mode == "helm"
    assert executor.helm_chart_path == "/opt/charts/generic-web-app"
    assert executor.helm_binary == "/usr/local/bin/helm"
    assert executor.helm_timeout == "240s"
    assert executor.ingress_enabled is True
    assert executor.ingress_class_name == "nginx"
    assert executor.ingress_base_domain == "127.0.0.1.nip.io"


def test_create_executor_rejects_invalid_kubernetes_deployment_mode(app):
    with app.app_context():
        app.config["CONTROL_PLANE_EXECUTOR"] = "kubernetes"
        app.config["CONTROL_PLANE_K8S_DEPLOYMENT_MODE"] = "invalid"

        with pytest.raises(ValueError, match="CONTROL_PLANE_K8S_DEPLOYMENT_MODE"):
            create_executor()


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
        "kubernetes-ingress",
    )
    assert contract.required_config == (
        "CONTROL_PLANE_REGISTRY_ENABLED=true",
        "CONTROL_PLANE_REGISTRY_URL",
        "CONTROL_PLANE_REGISTRY_NAMESPACE",
        "CONTROL_PLANE_KUBECONFIG",
    )
    assert contract.optional_config == (
        "CONTROL_PLANE_K8S_NAMESPACE",
        "CONTROL_PLANE_K8S_IMAGE_PULL_SECRET",
        "CONTROL_PLANE_K8S_DEPLOYMENT_MODE",
        "CONTROL_PLANE_K8S_HELM_CHART_PATH",
        "CONTROL_PLANE_K8S_HELM_BINARY",
        "CONTROL_PLANE_K8S_HELM_TIMEOUT",
        "CONTROL_PLANE_K8S_INGRESS_ENABLED",
        "CONTROL_PLANE_K8S_INGRESS_CLASS_NAME",
        "CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN",
        "CONTROL_PLANE_REGISTRY_USERNAME",
        "CONTROL_PLANE_REGISTRY_PASSWORD",
        "CONTROL_PLANE_HEALTHCHECK_TIMEOUT_SECONDS",
        "CONTROL_PLANE_HEALTHCHECK_INTERVAL_SECONDS",
        "CONTROL_PLANE_CLAIM_REFRESH_INTERVAL_SECONDS",
    )


def test_kubernetes_executor_helm_mode_deploys_with_generated_values_and_release_name(tmp_path, monkeypatch):
    RecordingHelmRunner.instances = []
    value_generator_calls = []
    release_name_calls = []

    def fake_values_generator(deployment, config):
        value_generator_calls.append({"deployment": deployment, "image_pull_secret": config.image_pull_secret})
        return {
            "image": {"repository": "localhost:32000/helm-app", "tag": "dev", "pullPolicy": "IfNotPresent", "pullSecrets": []},
            "replicaCount": 1,
            "container": {"port": 3000},
            "env": [{"name": "APP_ENV", "value": "production"}],
            "envFrom": {"configMaps": [], "secrets": []},
            "service": {"type": "ClusterIP", "port": 3000, "targetPort": ""},
            "resources": {},
            "probes": {"readiness": {"enabled": True}, "liveness": {"enabled": True}, "startup": {"enabled": False}},
            "ingress": {"enabled": False},
        }

    def fake_release_name(project, deployment):
        release_name_calls.append({"project": project, "deployment": deployment})
        return "paas-helm-app-production-3"

    monkeypatch.setattr("worker.executor.generic_web_app_values", fake_values_generator)
    monkeypatch.setattr("worker.executor.helm_release_name", fake_release_name)

    kubectl_commands = []

    def fake_runner(args, **kwargs):
        kubectl_commands.append(args)
        raise AssertionError(f"Manifest-mode kubectl command should not run in helm deploy mode: {args}")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        port_allocator=lambda: 19090,
        health_probe=lambda _url: {"status_code": 200, "summary": "ok"},
        sleep_fn=lambda _seconds: None,
        registry_enabled=True,
        registry_url="localhost:32000",
        registry_namespace="",
        namespace="apps",
        image_pull_secret="registry-pull-secret",
        deployment_mode="helm",
        helm_chart_path="deploy/helm/generic-web-app",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub()

    result = executor.deploy(deployment)

    assert value_generator_calls == [{"deployment": deployment, "image_pull_secret": "registry-pull-secret"}]
    assert release_name_calls == [{"project": deployment.project, "deployment": deployment}]
    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.namespace == "apps"
    assert helm_runner.chart_path == "deploy/helm/generic-web-app"
    assert helm_runner.upgrade_install_calls[0]["release"] == "paas-helm-app-production-3"
    values_file = Path(helm_runner.upgrade_install_calls[0]["values_file"])
    assert values_file.name == "generic-web-app-values.yaml"
    assert json.loads(values_file.read_text(encoding="utf-8"))["container"]["port"] == 3000
    assert result.metadata["deployment_mode"] == "helm"
    assert result.metadata["helm_release_name"] == "paas-helm-app-production-3"
    assert result.metadata["namespace"] == "apps"
    assert result.metadata["chart_path"] == "deploy/helm/generic-web-app"
    assert result.service_url is None
    assert result.metadata["internal_service_url"] == "http://paas-helm-app-production-3-generic-web-app.apps.svc.cluster.local:3000"
    event_types = [event["event_type"] for event in result.events]
    assert "kubernetes.helm_deploy_started" in event_types
    assert "kubernetes.helm_deploy_succeeded" in event_types
    assert "kubernetes.healthcheck_succeeded" in event_types
    assert len(kubectl_commands) == 1
    assert "get" in kubectl_commands[0]
    assert "pods" in kubectl_commands[0]
    assert "json" in kubectl_commands[0]


def test_kubernetes_executor_helm_mode_failure_raises_worker_execution_error(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        registry_enabled=True,
        registry_url="localhost:32000",
        registry_namespace="",
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=FailingHelmRunner,
        popen_factory=DummyPopen,
    )

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor.deploy(make_kubernetes_deployment_stub())

    assert exc_info.value.step == "deploy.kubernetes.helm"
    assert exc_info.value.metadata["deployment_mode"] == "helm"
    assert exc_info.value.metadata["helm_returncode"] == 1
    assert "helm failed" in exc_info.value.metadata["helm_stderr_summary"]
    failed_event = next(event for event in exc_info.value.events if event["event_type"] == "kubernetes.helm_deploy_failed")
    assert failed_event["metadata_json"]["deployment_mode"] == "helm"


def test_kubernetes_executor_manifest_mode_stop_still_uses_kubectl_delete(tmp_path):
    commands = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="deleted\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        deployment_mode="manifest",
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="stop-app")

    result = executor.stop(deployment)

    assert result.metadata["stopped"] is True
    assert any(command[:4] == ["kubectl", "--namespace", "default", "delete"] for command in commands)
    assert commands[0][-2:] == ["--ignore-not-found=true", "--wait=false"]


def test_kubernetes_executor_helm_mode_stop_uninstalls_release(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="stop-app", project_id=3)

    result = executor.stop(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.namespace == "apps"
    assert helm_runner.uninstall_call == {"release": "paas-stop-app-production-3"}
    assert result.deploy_target == "kubernetes"
    assert result.metadata["stopped"] is True
    assert result.metadata["deployment_mode"] == "helm"
    assert result.metadata["helm_release_name"] == "paas-stop-app-production-3"
    event_types = [event["event_type"] for event in result.events]
    assert event_types == ["kubernetes.helm_uninstall_started", "kubernetes.helm_uninstall_succeeded"]


def test_kubernetes_executor_helm_mode_stop_uses_recorded_release_name_when_available(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="renamed-app", project_id=3)
    deployment.events = [
        SimpleNamespace(id=1, metadata_json={"helm_release_name": "paas-original-name-production-3"})
    ]

    result = executor.stop(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.uninstall_call == {"release": "paas-original-name-production-3"}
    assert result.metadata["helm_release_name"] == "paas-original-name-production-3"


def test_kubernetes_executor_helm_mode_stop_prefers_persisted_release_name(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="renamed-app", project_id=3)
    deployment.helm_release_name = "paas-persisted-name-production-3"
    deployment.events = [
        SimpleNamespace(id=1, metadata_json={"helm_release_name": "paas-event-name-production-3"})
    ]

    result = executor.stop(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.uninstall_call == {"release": "paas-persisted-name-production-3"}
    assert result.metadata["helm_release_name"] == "paas-persisted-name-production-3"


def test_kubernetes_executor_stop_uses_helm_when_release_metadata_is_persisted(tmp_path):
    RecordingHelmRunner.instances = []

    def fake_runner(args, **kwargs):
        raise AssertionError(f"Manifest-mode kubectl command should not run for Helm metadata stop: {args}")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        namespace="apps",
        deployment_mode="manifest",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="persisted-stop-app", project_id=3)
    deployment.helm_release_name = "paas-persisted-stop-app-production-3"

    result = executor.stop(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.uninstall_call == {"release": "paas-persisted-stop-app-production-3"}
    assert result.metadata["helm_release_name"] == "paas-persisted-stop-app-production-3"


def test_kubernetes_executor_helm_runtime_status_reports_existing_release(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=RecordingHelmRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="status-app", project_id=3)
    deployment.helm_release_name = "paas-status-app-production-3"

    status = executor.runtime_helm_status(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.status_calls == [{"release": "paas-status-app-production-3"}]
    assert status["release_exists"] is True
    assert status["release_status"] == "deployed"
    assert status["helm_release_name"] == "paas-status-app-production-3"
    assert status["namespace"] == "apps"


def test_kubernetes_executor_helm_runtime_status_reports_missing_release(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=MissingHelmReleaseRunner,
        popen_factory=DummyPopen,
    )
    deployment = make_kubernetes_deployment_stub(deployment_id=9, name="status-missing-app", project_id=3)
    deployment.helm_release_name = "paas-status-missing-app-production-3"

    status = executor.runtime_helm_status(deployment)

    helm_runner = RecordingHelmRunner.instances[0]
    assert helm_runner.status_calls == [{"release": "paas-status-missing-app-production-3"}]
    assert status["release_exists"] is False
    assert status["helm_release_name"] == "paas-status-missing-app-production-3"
    assert status["namespace"] == "apps"


def test_kubernetes_executor_helm_mode_stop_failure_raises_worker_execution_error(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=FailingHelmUninstallRunner,
        popen_factory=DummyPopen,
    )

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor.stop(make_kubernetes_deployment_stub(deployment_id=9, name="stop-app", project_id=3))

    assert exc_info.value.step == "deploy.kubernetes.helm_uninstall"
    assert exc_info.value.metadata["helm_returncode"] == 1
    assert "uninstall failed" in exc_info.value.metadata["helm_stderr_summary"]
    event_types = [event["event_type"] for event in exc_info.value.events]
    assert event_types == ["kubernetes.helm_uninstall_started", "kubernetes.helm_uninstall_failed"]


def test_kubernetes_executor_helm_mode_stop_treats_release_not_found_as_stopped(tmp_path):
    RecordingHelmRunner.instances = []
    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        namespace="apps",
        deployment_mode="helm",
        helm_runner_factory=MissingHelmReleaseRunner,
        popen_factory=DummyPopen,
    )

    result = executor.stop(make_kubernetes_deployment_stub(deployment_id=9, name="stop-app", project_id=3))

    assert result.metadata["stopped"] is True
    assert result.metadata["release_not_found"] is True
    event_types = [event["event_type"] for event in result.events]
    assert event_types == ["kubernetes.helm_uninstall_started", "kubernetes.helm_uninstall_not_found"]


def test_kubernetes_executor_helm_mode_does_not_change_runtime_status_behavior(tmp_path):
    commands = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="found\n", stderr="")

    executor = KubernetesExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        deployment_mode="helm",
        popen_factory=DummyPopen,
    )

    status = executor.runtime_resource_status(make_kubernetes_deployment_stub(deployment_id=10, name="status-app"))

    assert status["deployment_exists"] is True
    assert status["service_exists"] is True
    assert any("deployment/paas-status-app-10" in command for command in commands)
    assert any("service/paas-status-app-10-svc" in command for command in commands)


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
        if args[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="manifest ok\n", stderr="")
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
    assert processed.service_url is None

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
                if "--previous" in args:
                    return subprocess.CompletedProcess(
                        args=args,
                        returncode=0,
                        stdout="Previous error: missing DATABASE_URL\n",
                        stderr="",
                    )
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
        assert (
            failed_event["metadata_json"]["rollout_pod_previous_logs_summary"]
            == "app-123: Previous error: missing DATABASE_URL"
        )
        assert exc.metadata["rollout_pods_summary"] == "NAME READY STATUS RESTARTS AGE | app-123 0/1 ImagePullBackOff 0 15s"
        assert exc.metadata["rollout_describe_summary"] == "Events: | Warning  Failed  15s  kubelet  Failed to pull image"
        assert exc.metadata["rollout_pod_describe_summary"] == "app-123: Pod Events: | Warning  Failed  15s  kubelet  Back-off pulling image"
        assert exc.metadata["rollout_pod_logs_summary"] == "app-123: Error: failed to start application"
        assert exc.metadata["rollout_pod_previous_logs_summary"] == "app-123: Previous error: missing DATABASE_URL"


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
