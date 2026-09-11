"""Deploy and remove Kubernetes workloads using rendered manifests."""

import json

from control_plane.deployment_spec import project_for_deployment
from control_plane.deployment_runtime_metadata import kubernetes_ingress_name
from worker.execution.contracts import ExecutionResult, WorkerExecutionError
from worker.executors.kubernetes.manifest_renderer import KubernetesManifestRendererMixin


class ManifestDeploymentMixin(KubernetesManifestRendererMixin):
    def _deploy_with_manifest(self, deployment):
        project = project_for_deployment(deployment)
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        manifest_path = logs_dir / "kubernetes-manifest.json"
        apply_log_path = logs_dir / "kubernetes-apply.log"
        rollout_log_path = logs_dir / "kubernetes-rollout.log"
        port_forward_log_path = logs_dir / "kubernetes-port-forward.log"
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        ingress_name = kubernetes_ingress_name(deployment) or deployment_name
        internal_service_url = self._service_url(service_name, project.port)
        ingress_host = self._ingress_host(deployment.id)
        service_url = self._ingress_url(ingress_host)
        effective_url = service_url or internal_service_url
        healthcheck_url = f"{effective_url}{project.healthcheck_path}"
        manifest = self._manifest(deployment, deployment_name=deployment_name, service_name=service_name)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        resource_metadata = {
            "deployment_name": deployment_name,
            "service_name": service_name,
            "ingress_name": ingress_name if self.ingress_enabled else None,
            "manifest_path": str(manifest_path),
            "namespace": self.namespace,
        }

        events = [
            self._event(
                "kubernetes.manifest_apply_started",
                "deploying",
                "Applying Kubernetes Deployment and Service manifests",
                step="deploy.kubernetes.apply",
                metadata=resource_metadata,
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
                    metadata=resource_metadata,
                ),
                failure_event_type="kubernetes.manifest_apply_failed",
                failure_step="deploy.kubernetes.apply",
                failure_metadata=resource_metadata,
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
                        "internal_service_url": internal_service_url,
                        "ingress_name": ingress_name if self.ingress_enabled else None,
                        "ingress_host": ingress_host,
                        "ingress_class": self.ingress_class_name or None,
                        "port": project.port,
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
            try:
                rollout_result = self._run_manifest_rollout_status(
                    deployment_name,
                    timeout_seconds=self.rollout_timeout,
                    log_path=rollout_log_path,
                    existing_events=events,
                )
            except WorkerExecutionError as exc:
                if not self._is_rollout_condition_timeout(exc):
                    raise
                rollout_result = self._run_manifest_rollout_status(
                    deployment_name,
                    timeout_seconds=15,
                    log_path=logs_dir / "kubernetes-rollout-recheck.log",
                    existing_events=events,
                    grace_recheck=True,
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
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "namespace": self.namespace,
                    "service_url": service_url,
                    "internal_service_url": internal_service_url,
                    "ingress_name": ingress_name if self.ingress_enabled else None,
                    "ingress_host": ingress_host,
                    "ingress_class": self.ingress_class_name or None,
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
                            "internal_service_url": internal_service_url,
                            "ingress_name": ingress_name if self.ingress_enabled else None,
                            "ingress_host": ingress_host,
                            "ingress_class": self.ingress_class_name or None,
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
                    "internal_service_url": internal_service_url,
                    "ingress_name": ingress_name if self.ingress_enabled else None,
                    "ingress_host": ingress_host,
                    "ingress_class": self.ingress_class_name or None,
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
                "internal_service_url": internal_service_url,
                "ingress_name": ingress_name if self.ingress_enabled else None,
                "ingress_host": ingress_host,
                "ingress_class": self.ingress_class_name or None,
                "healthcheck_url": healthcheck_url,
                "image_ref": deployment.build.image_ref,
                **self._env_source_summary(project.env_vars),
                **health_metadata,
            },
            events=events,
            log_path=str(rollout_log_path),
            service_url=service_url,
            deploy_target=self.deploy_target,
            healthcheck_url=healthcheck_url,
            runtime_log_path=health_metadata.get("runtime_log_path"),
        )

    def _run_manifest_rollout_status(
        self,
        deployment_name,
        *,
        timeout_seconds,
        log_path,
        existing_events,
        grace_recheck=False,
    ):
        message = (
            "Kubernetes rollout completed during grace recheck"
            if grace_recheck
            else "Kubernetes rollout completed successfully"
        )
        return self._run_kubectl_with_events(
            "deploy.kubernetes.rollout",
            self._kubectl_args(
                "rollout",
                "status",
                f"deployment/{deployment_name}",
                "--timeout",
                f"{timeout_seconds}s",
            ),
            log_path=log_path,
            success_event=self._event(
                "kubernetes.rollout_succeeded",
                "deploying",
                message,
                step="deploy.kubernetes.rollout",
                metadata={
                    "deployment_name": deployment_name,
                    "namespace": self.namespace,
                    "grace_recheck": grace_recheck,
                    "timeout_seconds": timeout_seconds,
                },
            ),
            failure_event_type="kubernetes.rollout_failed",
            failure_step="deploy.kubernetes.rollout",
            failure_metadata={
                "deployment_name": deployment_name,
                "namespace": self.namespace,
                "grace_recheck": grace_recheck,
                "timeout_seconds": timeout_seconds,
            },
            existing_events=existing_events,
        )

    @staticmethod
    def _is_rollout_condition_timeout(error):
        text = " ".join(
            str(value or "")
            for value in (
                error.message,
                error.metadata.get("summary"),
                " ".join(error.metadata.get("output_tail") or []),
            )
        ).lower()
        return "timed out waiting for the condition" in text


    def _stop_with_manifest(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "kubernetes-delete.log"
        namespace = self._namespace_for_deployment(deployment)
        deployment_name = self._k8s_deployment_name(deployment)
        service_name = self._k8s_service_name(deployment)
        ingress_name = kubernetes_ingress_name(deployment) or deployment_name
        start_event = self._event(
            "kubernetes.resources_delete_started",
            "stopped",
            "Deleting Kubernetes Deployment, Service, and Ingress resources",
            step="deploy.kubernetes.delete",
            metadata={
                "deployment_name": deployment_name,
                "service_name": service_name,
                "ingress_name": ingress_name,
                "namespace": namespace,
            },
        )
        delete_result = self._run_kubectl_with_events(
            "deploy.kubernetes.delete",
            self._kubectl_args(
                "delete",
                f"deployment/{deployment_name}",
                f"service/{service_name}",
                f"ingress/{ingress_name}",
                "--ignore-not-found=true",
                "--wait=false",
                namespace=namespace,
            ),
            log_path=log_path,
            success_event=self._event(
                "kubernetes.resources_deleted",
                "stopped",
                "Kubernetes resources deleted successfully",
                step="deploy.kubernetes.delete",
                metadata={
                    "deployment_name": deployment_name,
                    "service_name": service_name,
                    "ingress_name": ingress_name,
                    "namespace": namespace,
                },
            ),
            failure_event_type="kubernetes.resources_delete_failed",
            failure_step="deploy.kubernetes.delete",
            failure_metadata={
                "deployment_name": deployment_name,
                "service_name": service_name,
                "ingress_name": ingress_name,
                "namespace": namespace,
            },
            existing_events=[start_event],
        )
        return ExecutionResult(
            "Kubernetes resources deleted successfully.",
            metadata={
                "executor": self.deploy_target,
                "deployment_name": deployment_name,
                "service_name": service_name,
                "ingress_name": ingress_name,
                "namespace": namespace,
                "stopped": True,
            },
            events=[start_event, delete_result.events[-1]],
            log_path=str(log_path),
            deploy_target=self.deploy_target,
        )
