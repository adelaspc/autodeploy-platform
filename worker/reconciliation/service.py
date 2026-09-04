"""Compare persisted deployment state with runtime state and repair safe drift."""

from __future__ import annotations

from flask import current_app

from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from worker.execution.factory import create_executor_for_deployment
from worker.reconciliation.artifacts import reconcile_failed_artifacts as handle_failed_artifacts
from worker.reconciliation.claims import reconcile_stale_claim
from worker.reconciliation.docker import reconcile_running_missing_container as handle_running_missing_container
from worker.reconciliation.helm import (
    reconcile_helm_nonrunning_release as handle_helm_nonrunning_release,
    reconcile_running_missing_helm_release as handle_running_missing_helm_release,
)
from worker.reconciliation.kubernetes import (
    reconcile_kubernetes_nonrunning_resources as handle_kubernetes_nonrunning_resources,
    reconcile_running_missing_kubernetes_resource as handle_running_missing_kubernetes_resource,
)
from worker.reconciliation.events import record_reconcile_event


class ReconciliationContext:
    """Reuse one executor while checking a single deployment for drift."""

    def __init__(self, deployment, *, executor_factory=create_executor_for_deployment):
        self.deployment_id = deployment.id
        self._executor_factory = executor_factory
        self._executor = None

    def executor_for(self, deployment):
        if self._executor is None:
            self._executor = self._executor_factory(deployment)
        return self._executor


def iter_reconcilable_deployments():
    return PlatformDeployment.query.order_by(PlatformDeployment.created_at.asc()).all()


def reconcile_running_missing_container(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_running_missing_container(deployment, executor_factory=executor_factory)


def reconcile_running_missing_helm_release(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_running_missing_helm_release(deployment, executor_factory=executor_factory)


def reconcile_helm_nonrunning_release(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_helm_nonrunning_release(deployment, executor_factory=executor_factory)


def reconcile_failed_artifacts(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_failed_artifacts(deployment, executor_factory=executor_factory)


def reconcile_running_missing_kubernetes_resource(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_running_missing_kubernetes_resource(deployment, executor_factory=executor_factory)


def reconcile_kubernetes_nonrunning_resources(deployment, *, executor_factory=create_executor_for_deployment):
    return handle_kubernetes_nonrunning_resources(deployment, executor_factory=executor_factory)


def reconcile_missing_deploy_target(deployment):
    if deployment.deploy_target:
        return False
    metadata = deployment.preflight_metadata_json or {}
    recovered_target = metadata.get("deploy_target")
    if recovered_target not in {"fake", "local-docker", "kubernetes"}:
        return False

    deployment.deploy_target = recovered_target
    record_reconcile_event(
        deployment,
        "reconcile.deploy_target_recovered",
        f"Recovered missing deployment target '{recovered_target}' from persisted preflight metadata",
        metadata={"deploy_target": recovered_target, "source": "preflight_metadata"},
    )
    db.session.commit()
    return True







def reconcile_deployment(deployment):
    # Earlier repairs may change the record consumed by later checks, so reload
    # after every change and keep the rule order explicit.
    changes = 0
    context = ReconciliationContext(deployment, executor_factory=create_executor_for_deployment)
    if reconcile_stale_claim(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_missing_deploy_target(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    handlers = (
        reconcile_running_missing_container,
        reconcile_running_missing_helm_release,
        reconcile_running_missing_kubernetes_resource,
        reconcile_helm_nonrunning_release,
        reconcile_kubernetes_nonrunning_resources,
        reconcile_failed_artifacts,
    )
    for handler in handlers:
        if handler(deployment, executor_factory=context.executor_for):
            deployment = db.session.get(PlatformDeployment, deployment.id)
            changes += 1
    return changes


def reconcile_deployments(*, emitter=None):
    emitter = emitter or (lambda _message: None)
    total_changes = 0
    for deployment in iter_reconcilable_deployments():
        try:
            changes = reconcile_deployment(deployment)
        except Exception as exc:
            current_app.logger.exception(
                "reconcile_deployment_failed",
                extra={
                    "event": "reconcile_deployment_failed",
                    "request_id": deployment.origin_request_id,
                    "project_id": deployment.project_id,
                    "deployment_id": deployment.id,
                    "build_id": deployment.build_id,
                    "error_type": type(exc).__name__,
                },
            )
            emitter(f"Deployment {deployment.id}: reconciliation error: {exc}")
            db.session.rollback()
            continue
        if changes:
            total_changes += changes
            current_app.logger.info(
                "reconcile_deployment_changed",
                extra={
                    "event": "reconcile_deployment_changed",
                    "request_id": deployment.origin_request_id,
                    "project_id": deployment.project_id,
                    "deployment_id": deployment.id,
                    "build_id": deployment.build_id,
                    "reconciliation_actions": changes,
                },
            )
            emitter(f"Deployment {deployment.id}: applied {changes} reconciliation action(s)")
    return total_changes
