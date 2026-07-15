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


def iter_reconcilable_deployments():
    return PlatformDeployment.query.order_by(PlatformDeployment.created_at.asc()).all()


def reconcile_running_missing_container(deployment):
    return handle_running_missing_container(deployment, executor_factory=create_executor_for_deployment)


def reconcile_running_missing_helm_release(deployment):
    return handle_running_missing_helm_release(deployment, executor_factory=create_executor_for_deployment)


def reconcile_helm_nonrunning_release(deployment):
    return handle_helm_nonrunning_release(deployment, executor_factory=create_executor_for_deployment)


def reconcile_failed_artifacts(deployment):
    return handle_failed_artifacts(deployment, executor_factory=create_executor_for_deployment)


def reconcile_running_missing_kubernetes_resource(deployment):
    return handle_running_missing_kubernetes_resource(deployment, executor_factory=create_executor_for_deployment)


def reconcile_kubernetes_nonrunning_resources(deployment):
    return handle_kubernetes_nonrunning_resources(deployment, executor_factory=create_executor_for_deployment)







def reconcile_deployment(deployment):
    changes = 0
    if reconcile_stale_claim(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_container(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_helm_release(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_running_missing_kubernetes_resource(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_helm_nonrunning_release(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_kubernetes_nonrunning_resources(deployment):
        deployment = db.session.get(PlatformDeployment, deployment.id)
        changes += 1
    if reconcile_failed_artifacts(deployment):
        changes += 1
    return changes


def reconcile_deployments(*, emitter=None):
    emitter = emitter or (lambda _message: None)
    total_changes = 0
    for deployment in iter_reconcilable_deployments():
        try:
            changes = reconcile_deployment(deployment)
        except Exception as exc:
            current_app.logger.exception("reconcile_deployment_failed", extra={"deployment_id": deployment.id})
            emitter(f"Deployment {deployment.id}: reconciliation error: {exc}")
            db.session.rollback()
            continue
        if changes:
            total_changes += changes
            emitter(f"Deployment {deployment.id}: applied {changes} reconciliation action(s)")
    return total_changes
