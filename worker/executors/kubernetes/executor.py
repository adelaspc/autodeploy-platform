"""Compose Kubernetes source, image, Helm, manifest, and diagnostic capabilities."""

from __future__ import annotations

from control_plane.deployment_spec import project_for_deployment
from control_plane.deployment_runtime_metadata import (
    kubernetes_deployment_mode,
    kubernetes_deployment_name,
    kubernetes_ingress_name,
    kubernetes_namespace,
    kubernetes_service_name,
)
from worker.execution.contracts import ExecutorContract
from worker.executors.local_docker import LocalDockerExecutor
from worker.executors.kubernetes.diagnostics import KubernetesDiagnosticsMixin
from worker.executors.kubernetes.healthcheck import PortForwardHealthcheckMixin
from worker.executors.kubernetes.helm import HelmDeploymentMixin
from worker.executors.kubernetes.manifest import ManifestDeploymentMixin
from worker.executors.kubernetes.names import manifest_deployment_name, manifest_service_name
from worker.executors.kubernetes.preflight import KubernetesPreflightMixin
from worker.helm.runner import HelmRunner


class KubernetesExecutor(
    ManifestDeploymentMixin,
    HelmDeploymentMixin,
    KubernetesPreflightMixin,
    KubernetesDiagnosticsMixin,
    PortForwardHealthcheckMixin,
    LocalDockerExecutor,
):
    deploy_target = "kubernetes"
    contract = ExecutorContract(
        name="kubernetes",
        deploy_target="kubernetes",
        runtime="kubernetes",
        healthcheck_strategy="service-port-forward",
        requires_registry_push=True,
        supports_runtime_logs=True,
        supports_runtime_reconciliation=True,
        managed_resources=("docker-image", "kubernetes-deployment", "kubernetes-service", "kubernetes-ingress"),
        required_config=(
            "CONTROL_PLANE_REGISTRY_ENABLED=true",
            "CONTROL_PLANE_REGISTRY_URL",
            "CONTROL_PLANE_REGISTRY_NAMESPACE",
            "CONTROL_PLANE_KUBECONFIG",
        ),
        optional_config=(
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
        deployment_mode="manifest",
        helm_chart_path="deploy/helm/generic-web-app",
        helm_binary="helm",
        helm_timeout="180s",
        helm_runner_factory=HelmRunner,
        ingress_enabled=False,
        ingress_class_name="",
        ingress_base_domain="127.0.0.1.nip.io",
        rollout_timeout=None,
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
        self.deployment_mode = self._normalize_deployment_mode(deployment_mode)
        self.helm_chart_path = helm_chart_path
        self.helm_binary = helm_binary
        self.helm_timeout = helm_timeout
        self.helm_runner_factory = helm_runner_factory
        self.ingress_enabled = bool(ingress_enabled)
        self.ingress_class_name = (ingress_class_name or "").strip()
        self.ingress_base_domain = (ingress_base_domain or "").strip().strip(".")
        self.rollout_timeout = healthcheck_timeout if rollout_timeout is None else rollout_timeout
        if self.ingress_enabled and not self.ingress_base_domain:
            raise ValueError("CONTROL_PLANE_K8S_INGRESS_BASE_DOMAIN is required when ingress is enabled")

    def deploy(self, deployment):
        if self.deployment_mode == "helm":
            return self._deploy_with_helm(deployment)
        return self._deploy_with_manifest(deployment)

    def stop(self, deployment):
        deployment_mode = kubernetes_deployment_mode(deployment) or self.deployment_mode
        if deployment_mode == "helm":
            return self._stop_with_helm(deployment)
        return self._stop_with_manifest(deployment)

    def runtime_resource_status(self, deployment):
        namespace = self._namespace_for_deployment(deployment)
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        ingress_name = kubernetes_ingress_name(deployment) or deployment_name
        deployment_exists = self._kubectl_resource_exists("deployment", deployment_name, namespace=namespace)
        service_exists = self._kubectl_resource_exists("service", service_name, namespace=namespace)
        ingress_exists = self._kubectl_resource_exists("ingress", ingress_name, namespace=namespace)
        return {
            "namespace": namespace,
            "deployment_name": deployment_name,
            "service_name": service_name,
            "deployment_exists": deployment_exists,
            "service_exists": service_exists,
            "ingress_name": ingress_name,
            "ingress_exists": ingress_exists,
        }

    def _kubectl_args(self, *parts, namespace=None):
        args = [self.kubectl_bin]
        if self.kubeconfig:
            args.extend(["--kubeconfig", self.kubeconfig])
        args.extend(["--namespace", namespace or self.namespace])
        args.extend(parts)
        return args

    def _namespace_for_deployment(self, deployment):
        return kubernetes_namespace(deployment) or self.namespace

    def _run_helm_command(
        self,
        args,
        *,
        capture_output=True,
        text=True,
        check=False,
        input=None,
        env=None,
        **_kwargs,
    ):
        """Run Helm through the executor command loop so claims stay alive."""
        return self._execute_command(
            args,
            stdin_input=input,
            env=env,
            timeout=self.command_timeout,
        )

    def _k8s_deployment_name(self, deployment):
        return kubernetes_deployment_name(deployment) or manifest_deployment_name(
            project_for_deployment(deployment), deployment
        )

    def _k8s_service_name(self, deployment):
        return kubernetes_service_name(deployment) or manifest_service_name(
            project_for_deployment(deployment), deployment
        )

    def kubernetes_resource_identity(self, deployment):
        deployment_name = self._k8s_deployment_name(deployment)
        return {
            "deployment_name": deployment_name,
            "service_name": self._k8s_service_name(deployment),
            "ingress_name": kubernetes_ingress_name(deployment) or deployment_name,
        }

    def _service_url(self, service_name, port):
        return f"http://{service_name}.{self.namespace}.svc.cluster.local:{port}"

    def _ingress_host(self, deployment_id):
        if not self.ingress_enabled:
            return None
        return f"paas-deployment-{self._alphabetic_id(deployment_id)}.{self.ingress_base_domain}"

    @staticmethod
    def _alphabetic_id(value):
        number = int(value)
        if number < 1:
            raise ValueError("deployment_id must be a positive integer")
        encoded = []
        while number:
            number, remainder = divmod(number - 1, 26)
            encoded.append(chr(ord("a") + remainder))
        return "".join(reversed(encoded))

    @staticmethod
    def _ingress_url(host):
        return f"http://{host}" if host else None

    @staticmethod
    def _normalize_deployment_mode(value):
        normalized = (value or "manifest").strip().lower()
        if normalized not in {"manifest", "helm"}:
            raise ValueError("CONTROL_PLANE_K8S_DEPLOYMENT_MODE must be one of: manifest, helm")
        return normalized
