"""Fail early when Kubernetes credentials or referenced resources are unavailable."""

import subprocess

from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import PreflightResult, WorkerExecutionError


class KubernetesPreflightMixin:
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
        project = project_for_deployment(deployment)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        references = self._kubernetes_referenced_resources(project.env_vars)
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

        if self.ingress_enabled:
            args = self._kubectl_args(
                "api-resources", "--api-group=networking.k8s.io", "--output=name"
            )
            result = self._execute_command(args, allow_heartbeat=False)
            commands.append(args)
            api_resource_names = set((result.stdout or "").split())
            ingress_api_available = result.returncode == 0 and bool(
                {"ingresses", "ingresses.networking.k8s.io"} & api_resource_names
            )
            checked_resources.append("api/ingresses.networking.k8s.io")
            log_lines.append(
                f"api/ingresses.networking.k8s.io: {'found' if ingress_api_available else 'missing'}"
            )
            if not ingress_api_available:
                missing_resources.append({"kind": "APIResource", "name": "ingresses.networking.k8s.io"})
            if self.ingress_class_name:
                exists = self._kubectl_resource_exists_for_preflight("ingressclass", self.ingress_class_name)
                checked_resources.append(f"ingressclass/{self.ingress_class_name}")
                log_lines.append(f"ingressclass/{self.ingress_class_name}: {'found' if exists else 'missing'}")
                if not exists:
                    missing_resources.append({"kind": "IngressClass", "name": self.ingress_class_name})

        self._write_log(log_path, commands, "\n".join(log_lines) + ("\n" if log_lines else ""))

        metadata = {
            "namespace": self.namespace,
            "preflight_log_path": str(log_path),
            "checked_resources": checked_resources,
            "missing_resources": missing_resources,
            "missing_resource_names": [item["name"] for item in missing_resources],
            "missing_resource_types": sorted({item["kind"] for item in missing_resources}),
            "image_pull_secret": references.get("image_pull_secret"),
            "ingress_enabled": self.ingress_enabled,
            "ingress_class": self.ingress_class_name or None,
            "ingress_base_domain": self.ingress_base_domain if self.ingress_enabled else None,
            **self._env_source_summary(project.env_vars),
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


    def _kubectl_resource_exists(self, kind, name, *, namespace=None):
        resolved_namespace = namespace or self.namespace
        try:
            result = self._execute_command(
                self._kubectl_args("get", f"{kind}/{name}", namespace=resolved_namespace),
                allow_heartbeat=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise WorkerExecutionError(
                "reconcile.kubernetes_resource_exists",
                f"Failed to inspect Kubernetes resource '{kind}/{name}': {exc}",
                metadata={"kind": kind, "name": name, "namespace": resolved_namespace},
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
