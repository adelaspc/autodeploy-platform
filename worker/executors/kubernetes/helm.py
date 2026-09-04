"""Implement Kubernetes deployment and cleanup through the bundled Helm chart."""

import json

from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import ExecutionResult, WorkerExecutionError
from worker.executors.kubernetes.names import helm_release_name
from worker.helm.runner import HelmCommandError
from worker.helm.values import GenericWebAppValuesConfig, generic_web_app_values


class HelmDeploymentMixin:
    def _deploy_with_helm(self, deployment):
        project = project_for_deployment(deployment)
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        values_path = logs_dir / "generic-web-app-values.yaml"
        helm_log_path = logs_dir / "helm-upgrade-install.log"
        port_forward_log_path = logs_dir / "kubernetes-port-forward.log"
        release_name = helm_release_name(project, deployment)
        service_name = self._helm_resource_name(release_name)
        deployment_name = service_name
        internal_service_url = self._service_url(service_name, project.port)
        ingress_host = self._ingress_host(deployment.id)
        service_url = self._ingress_url(ingress_host)
        effective_url = service_url or internal_service_url
        healthcheck_url = f"{effective_url}{project.healthcheck_path}"
        values = generic_web_app_values(
            deployment,
            GenericWebAppValuesConfig(
                image_pull_secret=self.image_pull_secret,
                ingress_enabled=self.ingress_enabled,
                ingress_host=ingress_host or "",
                ingress_class_name=self.ingress_class_name,
            ),
        )
        values_path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")

        metadata = {
            "deployment_mode": "helm",
            "helm_release_name": release_name,
            "namespace": self.namespace,
            "chart_path": self.helm_chart_path,
            "values_path": str(values_path),
            "internal_service_url": internal_service_url,
            "ingress_name": service_name if self.ingress_enabled else None,
            "ingress_host": ingress_host,
            "ingress_class": self.ingress_class_name or None,
        }
        events = [
            self._event(
                "kubernetes.helm_deploy_started",
                "deploying",
                f"Deploying Helm release '{release_name}'",
                step="deploy.kubernetes.helm",
                metadata=metadata,
            )
        ]
        helm_runner = self.helm_runner_factory(
            namespace=self.namespace,
            helm_binary=self.helm_binary,
            chart_path=self.helm_chart_path,
            helm_timeout=self.helm_timeout,
            kubeconfig=self.kubeconfig,
        )

        try:
            helm_result = helm_runner.upgrade_install(release_name, str(values_path))
        except HelmCommandError as exc:
            output = "\n".join(part for part in (exc.result.stdout, exc.result.stderr) if part)
            self._write_log(helm_log_path, exc.result.args, output)
            failure_metadata = metadata | {
                "helm_args": exc.result.args,
                "helm_returncode": exc.result.returncode,
                "helm_stdout_summary": self._summarize_output(exc.result.stdout),
                "helm_stderr_summary": self._summarize_output(exc.result.stderr),
                "helm_log_path": str(helm_log_path),
            }
            raise WorkerExecutionError(
                "deploy.kubernetes.helm",
                f"Helm release '{release_name}' failed to deploy",
                metadata=failure_metadata,
                log_path=str(helm_log_path),
                events=events
                + [
                    self._event(
                        "kubernetes.helm_deploy_failed",
                        "failed",
                        f"Helm release '{release_name}' failed to deploy",
                        step="deploy.kubernetes.helm",
                        level="error",
                        metadata=failure_metadata,
                    )
                ],
            ) from exc

        helm_output = "\n".join(part for part in (helm_result.stdout, helm_result.stderr) if part)
        self._write_log(helm_log_path, helm_result.args, helm_output)
        helm_metadata = metadata | {
            "helm_args": helm_result.args,
            "helm_returncode": helm_result.returncode,
            "helm_stdout_summary": self._summarize_output(helm_result.stdout),
            "helm_stderr_summary": self._summarize_output(helm_result.stderr),
            "helm_log_path": str(helm_log_path),
            "service_name": service_name,
            "deployment_name": deployment_name,
            "service_url": service_url,
            "internal_service_url": internal_service_url,
            "healthcheck_url": healthcheck_url,
        }
        events.append(
            self._event(
                "kubernetes.helm_deploy_succeeded",
                "deploying",
                f"Helm release '{release_name}' deployed successfully",
                step="deploy.kubernetes.helm",
                metadata=helm_metadata,
            )
        )
        events.append(
            self._event(
                "kubernetes.healthcheck_started",
                "deploying",
                f"Waiting for healthcheck on Service '{service_name}'",
                step="deploy.kubernetes.healthcheck",
                metadata={"service_name": service_name, "namespace": self.namespace, "healthcheck_url": healthcheck_url},
            )
        )

        # Health checks use a temporary Service port-forward. The public ingress
        # may depend on local DNS or routing that is not available to the worker.
        try:
            health_metadata = self._port_forward_healthcheck(
                deployment,
                service_name=service_name,
                log_path=port_forward_log_path,
            )
            health_metadata |= self._collect_pod_runtime_metadata(
                deployment_name, prefix="healthcheck", logs_dir=logs_dir
            )
            health_metadata |= self._capture_runtime_logs(
                deployment, deployment_name, logs_dir=logs_dir
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
                    **metadata,
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "service_url": service_url,
                    "healthcheck_url": healthcheck_url,
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
                            **metadata,
                            "deployment_name": deployment_name,
                            "service_name": service_name,
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
                    **metadata,
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "service_url": service_url,
                    "healthcheck_url": healthcheck_url,
                },
            )
        )

        return ExecutionResult(
            "Kubernetes Helm release deployed and passed healthcheck.",
            metadata={
                "executor": self.deploy_target,
                **helm_metadata,
                "image_ref": deployment.build.image_ref,
                **self._env_source_summary(project.env_vars),
                **health_metadata,
            },
            events=events,
            log_path=str(helm_log_path),
            service_url=service_url,
            deploy_target=self.deploy_target,
            healthcheck_url=healthcheck_url,
            runtime_log_path=health_metadata.get("runtime_log_path"),
        )


    def _stop_with_helm(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "helm-uninstall.log"
        release_name = self._helm_release_name_for_deployment(deployment)
        metadata = {
            "deployment_mode": "helm",
            "helm_release_name": release_name,
            "namespace": self.namespace,
            "chart_path": self.helm_chart_path,
            "helm_log_path": str(log_path),
        }
        start_event = self._event(
            "kubernetes.helm_uninstall_started",
            "stopped",
            f"Uninstalling Helm release '{release_name}'",
            step="deploy.kubernetes.helm_uninstall",
            metadata=metadata,
        )
        helm_runner = self.helm_runner_factory(
            namespace=self.namespace,
            helm_binary=self.helm_binary,
            chart_path=self.helm_chart_path,
            helm_timeout=self.helm_timeout,
            kubeconfig=self.kubeconfig,
        )

        try:
            helm_result = helm_runner.uninstall(release_name)
        except HelmCommandError as exc:
            output = "\n".join(part for part in (exc.result.stdout, exc.result.stderr) if part)
            self._write_log(log_path, exc.result.args, output)
            result_metadata = metadata | {
                "helm_args": exc.result.args,
                "helm_returncode": exc.result.returncode,
                "helm_stdout_summary": self._summarize_output(exc.result.stdout),
                "helm_stderr_summary": self._summarize_output(exc.result.stderr),
            }
            if self._helm_release_not_found(exc.result.stderr):
                return ExecutionResult(
                    f"Helm release '{release_name}' was already absent.",
                    metadata=result_metadata | {"stopped": True, "release_not_found": True},
                    events=[
                        start_event,
                        self._event(
                            "kubernetes.helm_uninstall_not_found",
                            "stopped",
                            f"Helm release '{release_name}' was already absent",
                            step="deploy.kubernetes.helm_uninstall",
                            metadata=result_metadata | {"release_not_found": True},
                        ),
                    ],
                    log_path=str(log_path),
                    deploy_target=self.deploy_target,
                )
            raise WorkerExecutionError(
                "deploy.kubernetes.helm_uninstall",
                f"Helm release '{release_name}' failed to uninstall",
                metadata=result_metadata,
                log_path=str(log_path),
                events=[
                    start_event,
                    self._event(
                        "kubernetes.helm_uninstall_failed",
                        "failed",
                        f"Helm release '{release_name}' failed to uninstall",
                        step="deploy.kubernetes.helm_uninstall",
                        level="error",
                        metadata=result_metadata,
                    ),
                ],
            ) from exc

        output = "\n".join(part for part in (helm_result.stdout, helm_result.stderr) if part)
        self._write_log(log_path, helm_result.args, output)
        success_metadata = metadata | {
            "helm_args": helm_result.args,
            "helm_returncode": helm_result.returncode,
            "helm_stdout_summary": self._summarize_output(helm_result.stdout),
            "helm_stderr_summary": self._summarize_output(helm_result.stderr),
            "stopped": True,
        }
        return ExecutionResult(
            "Helm release uninstalled successfully.",
            metadata=success_metadata,
            events=[
                start_event,
                self._event(
                    "kubernetes.helm_uninstall_succeeded",
                    "stopped",
                    f"Helm release '{release_name}' uninstalled successfully",
                    step="deploy.kubernetes.helm_uninstall",
                    metadata=success_metadata,
                ),
            ],
            log_path=str(log_path),
            deploy_target=self.deploy_target,
        )


    def runtime_helm_status(self, deployment):
        release_name = self._helm_release_name_for_deployment(deployment)
        helm_runner = self.helm_runner_factory(
            namespace=self.namespace,
            helm_binary=self.helm_binary,
            chart_path=self.helm_chart_path,
            helm_timeout=self.helm_timeout,
            kubeconfig=self.kubeconfig,
        )
        try:
            result = helm_runner.status(release_name)
        except HelmCommandError as exc:
            if self._helm_release_not_found(exc.result.stderr):
                return {
                    "release_exists": False,
                    "helm_release_name": release_name,
                    "namespace": self.namespace,
                    "chart_path": self.helm_chart_path,
                    "helm_args": exc.result.args,
                    "helm_returncode": exc.result.returncode,
                    "helm_stdout_summary": self._summarize_output(exc.result.stdout),
                    "helm_stderr_summary": self._summarize_output(exc.result.stderr),
                }
            raise WorkerExecutionError(
                "reconcile.helm_status",
                f"Failed to inspect Helm release '{release_name}'",
                metadata={
                    "helm_release_name": release_name,
                    "namespace": self.namespace,
                    "chart_path": self.helm_chart_path,
                    "helm_args": exc.result.args,
                    "helm_returncode": exc.result.returncode,
                    "helm_stdout_summary": self._summarize_output(exc.result.stdout),
                    "helm_stderr_summary": self._summarize_output(exc.result.stderr),
                },
            ) from exc

        release_status = None
        if result.stdout.strip():
            try:
                release_status = json.loads(result.stdout).get("info", {}).get("status")
            except json.JSONDecodeError:
                release_status = None
        return {
            "release_exists": True,
            "helm_release_name": release_name,
            "namespace": self.namespace,
            "chart_path": self.helm_chart_path,
            "release_status": release_status,
            "helm_args": result.args,
            "helm_returncode": result.returncode,
            "helm_stdout_summary": self._summarize_output(result.stdout),
            "helm_stderr_summary": self._summarize_output(result.stderr),
        }


    @staticmethod
    def _helm_resource_name(release_name):
        return f"{release_name}-generic-web-app"[:63].rstrip("-")

    def _helm_release_name_for_deployment(self, deployment):
        # Prefer the recorded name so later naming changes do not orphan existing
        # releases. Event metadata covers deployments created before the column.
        persisted_release_name = getattr(deployment, "helm_release_name", None)
        if persisted_release_name:
            return str(persisted_release_name)
        for event in sorted(getattr(deployment, "events", []) or [], key=lambda item: getattr(item, "id", 0), reverse=True):
            metadata = getattr(event, "metadata_json", None) or {}
            release_name = metadata.get("helm_release_name") if isinstance(metadata, dict) else None
            if release_name:
                return str(release_name)
        return helm_release_name(project_for_deployment(deployment), deployment)

    def _deployment_has_helm_release_metadata(self, deployment):
        if getattr(deployment, "helm_release_name", None):
            return True
        for event in getattr(deployment, "events", []) or []:
            metadata = getattr(event, "metadata_json", None) or {}
            if isinstance(metadata, dict) and metadata.get("helm_release_name"):
                return True
        return False

    @staticmethod
    def _helm_release_not_found(stderr):
        normalized = (stderr or "").strip().lower()
        return "release: not found" in normalized or "release not loaded" in normalized
