"""Run claimed deployments through the ordered build-and-deploy pipeline."""

from flask import current_app

from control_plane.deployment_runtime_metadata import persist_kubernetes_runtime_identity
from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from sqlalchemy import update  # noqa: F401 - compatibility for existing pipeline consumers
from worker.execution.contracts import WorkerExecutionError
from worker.execution.factory import create_executor
from worker.processing.claims import (
    ClaimLostError,
    DeploymentCancellationRequested,
    attach_claim_heartbeat,
    claim_next_pending_deployment,
    ensure_claim_owned,
    now_utc,
    persist_claim_loss,
    persist_cancellation_acknowledgement,
    refresh_claim,
    release_deployment_claim,
    worker_id,
    write_claim_event,
)
from worker.processing.events import record_auxiliary_events, record_event
from worker.processing.lifecycle import (
    apply_execution_result,
    begin_step,
    clear_preflight_state,
    commit_step_result,
    mark_failed,
    persist_preflight_failure,
    persist_preflight_result,
    push_event_metadata,
)


def process_deployment(deployment, executor=None):
    """Run one claimed deployment through the shared build and deploy lifecycle."""
    current_app.logger.info(
        "deployment_worker_started",
        extra={
            "event": "deployment_worker_started",
            "request_id": deployment.origin_request_id,
            "project_id": deployment.project_id,
            "deployment_id": deployment.id,
            "build_id": deployment.build_id,
        },
    )
    executor = executor or create_executor()
    attach_claim_heartbeat(executor, deployment, expected_worker_id=worker_id())
    deployment.last_error = None
    deployment.build.last_error = None
    deployment.build.registry_push_status = None
    clear_preflight_state(deployment)

    try:
        begin_step(
            deployment,
            "cloning",
            event_type="repository.clone_started",
            message="Worker started cloning repository",
        )
        clone_result = executor.clone_repo(deployment)
        # Executor calls can take long enough for another worker to reclaim stale
        # work. Never persist their result until ownership has been checked again.
        ensure_claim_owned(deployment)
        apply_execution_result(deployment, clone_result)
        commit_step_result(
            deployment,
            event_type="repository.clone_succeeded",
            status=deployment.status,
            message=clone_result.message,
            step="repository.clone",
            metadata=clone_result.metadata | {"log_path": clone_result.log_path, "summary": clone_result.message},
            extra_events=clone_result.events,
        )

        begin_step(
            deployment,
            "building",
            event_type="image.build_started",
            message="Worker started building image",
        )
        build_result = executor.build_image(deployment)
        ensure_claim_owned(deployment)
        deployment.build.build_log_path = build_result.log_path
        apply_execution_result(deployment, build_result)
        commit_step_result(
            deployment,
            event_type="image.build_succeeded",
            status=deployment.status,
            message=build_result.message,
            step="image.build",
            metadata=build_result.metadata
            | {
                "log_path": build_result.log_path,
                "workspace_path": build_result.workspace_path,
                "image_tag": build_result.image_tag,
                "image_ref": build_result.image_ref,
                "summary": build_result.message,
            },
            extra_events=build_result.events,
        )

        # A deployment without a test command skips this state entirely.
        if deployment.build.test_command:
            begin_step(
                deployment,
                "testing",
                event_type="tests.started",
                message=f"Worker started tests with '{deployment.build.test_command}'",
            )
            test_result = executor.run_tests(deployment)
            ensure_claim_owned(deployment)
            apply_execution_result(deployment, test_result)
            commit_step_result(
                deployment,
                event_type="tests.succeeded",
                status="testing",
                message=test_result.message,
                step="tests",
                metadata=test_result.metadata | {"log_path": test_result.log_path, "summary": test_result.message},
                extra_events=test_result.events,
            )

        begin_step(
            deployment,
            "pushing_image",
            event_type="image.push_started",
            message="Worker started pushing image",
        )
        record_event(
            deployment,
            "image.tag_started",
            deployment.status,
            "Worker started tagging image for registry push",
            step="image.tag",
        )
        db.session.commit()
        tag_result = executor.tag_image(deployment)
        ensure_claim_owned(deployment)
        apply_execution_result(deployment, tag_result)
        commit_step_result(
            deployment,
            event_type="image.tag_succeeded",
            status=deployment.status,
            message=tag_result.message,
            step="image.tag",
            metadata=tag_result.metadata
            | {
                "log_path": tag_result.log_path,
                "image_tag": tag_result.image_tag,
                "image_ref": tag_result.image_ref,
                "summary": tag_result.message,
            },
            extra_events=tag_result.events,
        )

        push_result = executor.push_image(deployment)
        ensure_claim_owned(deployment)
        apply_execution_result(deployment, push_result)
        deployment.build.registry_push_status = "skipped" if push_result.metadata.get("skipped") else "succeeded"
        deployment.build.transition_to("succeeded")
        deployment.build.finished_at = now_utc()
        commit_step_result(
            deployment,
            event_type="image.push_succeeded",
            status="succeeded",
            message=push_result.message,
            step="image.push",
            metadata=push_event_metadata(push_result, push_result.message, status="succeeded")
            | {"log_path": push_result.log_path, "summary": push_result.message},
            extra_events=push_result.events,
        )

        record_event(
            deployment,
            "image.verify_started",
            deployment.status,
            "Worker started verifying registry image",
            step="image.verify",
            metadata={"image_ref": deployment.build.image_ref},
        )
        db.session.commit()
        verify_result = executor.verify_image(deployment)
        ensure_claim_owned(deployment)
        apply_execution_result(deployment, verify_result)
        commit_step_result(
            deployment,
            event_type="image.verify_succeeded",
            status=deployment.status,
            message=verify_result.message,
            step="image.verify",
            metadata=verify_result.metadata
            | {
                "log_path": verify_result.log_path,
                "step": "image.verify",
                "image_tag": verify_result.image_tag,
                "image_ref": verify_result.image_ref,
                "summary": verify_result.message,
                "skipped": bool(verify_result.metadata.get("skipped")),
            },
            extra_events=verify_result.events,
        )

        begin_step(
            deployment,
            "deploying",
            event_type="deployment.apply_started",
            message="Worker started deployment apply step",
        )
        deploy_target = getattr(executor, "deploy_target", None)
        if deploy_target and deploy_target != "unknown":
            ensure_claim_owned(deployment)
            deployment.deploy_target = deploy_target
            if deploy_target == "kubernetes":
                deployment_mode = getattr(
                    executor,
                    "deployment_mode",
                    current_app.config.get("CONTROL_PLANE_K8S_DEPLOYMENT_MODE", "manifest"),
                )
                resource_identity = {}
                if deployment_mode == "manifest":
                    resource_identity_fn = getattr(executor, "kubernetes_resource_identity", None)
                    if callable(resource_identity_fn):
                        resource_identity = resource_identity_fn(deployment)
                persist_kubernetes_runtime_identity(
                    deployment,
                    deployment_mode=deployment_mode,
                    namespace=getattr(
                        executor,
                        "namespace",
                        current_app.config.get("CONTROL_PLANE_K8S_NAMESPACE", "default"),
                    ),
                    **resource_identity,
                )
            db.session.commit()
        preflight_fn = getattr(executor, "preflight_deploy", None)
        if callable(preflight_fn):
            preflight_result = preflight_fn(deployment)
            ensure_claim_owned(deployment)
            persist_preflight_result(deployment, preflight_result)
            if preflight_result is not None:
                commit_step_result(
                    deployment,
                    event_type="deployment.preflight_succeeded",
                    status="deploying",
                    message=preflight_result.summary,
                    step="deploy.preflight",
                    metadata=preflight_result.metadata
                    | {
                        "log_path": preflight_result.log_path,
                        "deploy_target": preflight_result.deploy_target,
                        "summary": preflight_result.summary,
                        "status": preflight_result.status,
                    },
                    extra_events=preflight_result.events,
                )
        deploy_result = executor.deploy(deployment)
        ensure_claim_owned(deployment)
        apply_execution_result(deployment, deploy_result)
        commit_step_result(
            deployment,
            event_type="deployment.apply_succeeded",
            status="deploying",
            message=deploy_result.message,
            step="deploy",
            metadata=deploy_result.metadata
            | {
                "log_path": deploy_result.log_path,
                "service_url": deploy_result.service_url,
                "deploy_target": deploy_result.deploy_target,
                "summary": deploy_result.message,
            },
            extra_events=deploy_result.events,
        )

        ensure_claim_owned(deployment)
        refresh_claim(deployment)
        deployment.transition_to("running")
        deployment.finished_at = now_utc()
        record_event(
            deployment,
            "deployment.running",
            "running",
            "Deployment is now running",
            step="deployment",
            metadata={
                "service_url": deployment.service_url,
                "deploy_target": deployment.deploy_target,
                "summary": "Deployment is now running",
            },
        )
        write_claim_event(
            deployment,
            "claim_cleared",
            "Worker cleared deployment claim after success",
            metadata={"worker_id": worker_id()},
        )
        release_deployment_claim(deployment)
        db.session.commit()
        return deployment
    except ClaimLostError as exc:
        return persist_claim_loss(deployment.id, exc)
    except DeploymentCancellationRequested as exc:
        return persist_cancellation_acknowledgement(deployment.id, exc)
    except WorkerExecutionError as exc:
        if exc.log_path:
            deployment.build.log_path = exc.log_path
            if exc.step == "image.build":
                deployment.build.build_log_path = exc.log_path
        if exc.step == "image.push":
            deployment.build.registry_push_status = "failed"
        persist_preflight_failure(deployment, exc)
        try:
            record_auxiliary_events(deployment, exc.events)
            return mark_failed(deployment, exc.step, exc.message, metadata=exc.metadata)
        except ClaimLostError as claim_exc:
            return persist_claim_loss(deployment.id, claim_exc)
        except DeploymentCancellationRequested as cancellation_exc:
            return persist_cancellation_acknowledgement(deployment.id, cancellation_exc)
    except Exception as exc:
        deployment_id = deployment.id
        current_app.logger.exception(
            "deployment_worker_unexpected_failure",
            extra={
                "deployment_id": deployment_id,
                "request_id": deployment.origin_request_id,
                "error_type": type(exc).__name__,
            },
        )
        db.session.rollback()
        deployment = db.session.get(PlatformDeployment, deployment_id)
        if deployment is None:
            return None
        try:
            return mark_failed(
                deployment,
                "worker.internal",
                "Worker encountered an unexpected internal error",
                metadata={"error_type": type(exc).__name__},
            )
        except ClaimLostError as claim_exc:
            return persist_claim_loss(deployment_id, claim_exc)
        except DeploymentCancellationRequested as cancellation_exc:
            return persist_cancellation_acknowledgement(deployment_id, cancellation_exc)


def process_next_pending_deployment(executor=None):
    deployment = claim_next_pending_deployment()
    if deployment is None:
        return None

    return process_deployment(deployment, executor=executor)
