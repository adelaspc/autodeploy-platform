from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import and_, or_, select, update

from backend.extensions import db
from backend.deployment_runtime_metadata import persist_helm_runtime_metadata
from backend.models import DeploymentEvent, PlatformDeployment
from backend.security import redact_sensitive_data, redact_text, secret_values_from_env_vars
from worker.executor import WorkerExecutionError, create_executor


BUILD_STATUS_BY_DEPLOYMENT_STATUS = {
    "cloning": "cloning",
    "building": "building",
    "testing": "testing",
    "pushing_image": "pushing_image",
}


def push_error_hint(message, metadata=None):
    haystack = " ".join(
        str(part)
        for part in [
            message or "",
            (metadata or {}).get("summary") or "",
            " ".join((metadata or {}).get("output_tail") or []),
        ]
        if part
    ).lower()
    if "denied: requested access to the resource is denied" in haystack:
        return {
            "possible_causes": [
                "registry authentication or token scope issue",
                "repository permission or namespace access issue",
                "registry plan or private repository limit",
            ]
        }
    if "denied:" in haystack:
        return {
            "possible_causes": [
                "registry authentication or token scope issue",
                "repository permission or namespace access issue",
                "registry plan or private repository limit",
            ]
        }
    if "unauthorized" in haystack or "insufficient scopes" in haystack:
        return {
            "possible_causes": [
                "registry authentication failed",
                "registry token does not have push permission",
            ]
        }
    if "repository does not exist" in haystack:
        return {
            "possible_causes": [
                "target repository does not exist",
                "authenticated user does not have permission to create or push to the repository",
            ]
        }
    return {}


def push_event_metadata(result, message, *, status):
    metadata = result.metadata | {
        "step": "image.push",
        "success": status == "succeeded",
        "push_log_available": bool(result.log_path),
        "push_log_path": result.log_path,
        "push_output_tail": result.metadata.get("output_tail"),
        "push_summary": result.metadata.get("summary") or message,
        "push_duration": result.metadata.get("duration_seconds"),
        "push_attempts": result.metadata.get("attempt"),
        "push_total_attempts": result.metadata.get("total_attempts"),
    }
    if status == "failed":
        metadata["error_message"] = message
        metadata |= push_error_hint(message, metadata)
    return metadata


class ClaimLostError(Exception):
    def __init__(self, deployment_id, worker_id, current_claimed_by, current_claimed_at, status):
        super().__init__("Deployment claim was lost before worker completed processing")
        self.deployment_id = deployment_id
        self.worker_id = worker_id
        self.current_claimed_by = current_claimed_by
        self.current_claimed_at = current_claimed_at
        self.status = status

    @property
    def metadata(self):
        return {
            "worker_id": self.worker_id,
            "current_claimed_by": self.current_claimed_by,
            "current_claimed_at": self.current_claimed_at.isoformat() if self.current_claimed_at else None,
        }


def now_utc():
    return datetime.now(timezone.utc)


def deployment_secret_values(deployment):
    return secret_values_from_env_vars(deployment.project.env_vars if deployment and deployment.project else [])


def worker_id():
    return current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")


def log_claim(action, deployment_id, **fields):
    current_app.logger.info("claim_%s", action, extra={"deployment_id": deployment_id, **fields})


def apply_execution_result(deployment, result):
    if result is None:
        return

    build = deployment.build
    if result.workspace_path:
        build.workspace_path = result.workspace_path
    if result.log_path:
        build.log_path = result.log_path
    if result.image_tag:
        build.image_tag = result.image_tag
    if result.image_ref:
        build.image_ref = result.image_ref
    if result.deploy_target:
        deployment.deploy_target = result.deploy_target
    if result.container_name:
        deployment.container_name = result.container_name
    if result.container_id:
        deployment.container_id = result.container_id
    if result.host_port is not None:
        deployment.host_port = result.host_port
    if result.healthcheck_url:
        deployment.healthcheck_url = result.healthcheck_url
    if result.service_url:
        deployment.service_url = result.service_url
    persist_helm_runtime_metadata(deployment, result.metadata)


def clear_preflight_state(deployment):
    deployment.preflight_status = None
    deployment.preflight_summary = None
    deployment.preflight_metadata_json = None
    deployment.preflight_completed_at = None


def persist_preflight_result(deployment, result):
    if result is None:
        return
    secret_values = deployment_secret_values(deployment)
    deployment.preflight_status = result.status
    deployment.preflight_summary = redact_text(result.summary, secret_values=secret_values)
    deployment.preflight_metadata_json = redact_sensitive_data(
        {
        **(result.metadata or {}),
        "log_path": result.log_path,
        "deploy_target": result.deploy_target,
        "summary": result.summary,
        "status": result.status,
        },
        secret_values=secret_values,
    )
    deployment.preflight_completed_at = now_utc()


def persist_preflight_failure(deployment, error):
    if "preflight" not in (error.step or ""):
        return
    secret_values = deployment_secret_values(deployment)
    deployment.preflight_status = "failed"
    deployment.preflight_summary = redact_text(error.message, secret_values=secret_values)
    deployment.preflight_metadata_json = redact_sensitive_data(
        {
        **(error.metadata or {}),
        "log_path": error.log_path,
        "summary": error.message,
        "status": "failed",
        },
        secret_values=secret_values,
    )
    deployment.preflight_completed_at = now_utc()


def record_event(deployment, event_type, status, message, *, step=None, level="info", metadata=None):
    secret_values = deployment_secret_values(deployment)
    db.session.add(
        DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            step=step,
            level=level,
            status=status,
            message=redact_text(message, secret_values=secret_values) if message else None,
            metadata_json=redact_sensitive_data(metadata, secret_values=secret_values),
        )
    )


def record_auxiliary_events(deployment, events):
    for event in events or ():
        record_event(
            deployment,
            event["event_type"],
            event["status"],
            event["message"],
            step=event.get("step"),
            level=event.get("level", "info"),
            metadata=event.get("metadata_json"),
        )


def write_claim_event(deployment, event_type, message, *, level="info", metadata=None):
    record_event(
        deployment,
        event_type,
        deployment.status,
        message,
        step="claim",
        level=level,
        metadata=metadata,
    )


def set_deployment_status(deployment, status, *, event_type, message):
    if deployment.started_at is None and status != "pending":
        deployment.started_at = now_utc()
    if deployment.build.started_at is None and status in BUILD_STATUS_BY_DEPLOYMENT_STATUS:
        deployment.build.started_at = now_utc()

    deployment.status = status
    build_status = BUILD_STATUS_BY_DEPLOYMENT_STATUS.get(status)
    if build_status:
        deployment.build.status = build_status
    record_event(deployment, event_type, status, message, step=status)
    db.session.flush()


def release_deployment_claim(deployment):
    previous_claimed_at = deployment.claimed_at
    previous_claimed_by = deployment.claimed_by
    deployment.claimed_at = None
    deployment.claimed_by = None
    log_claim(
        "cleared",
        deployment.id,
        worker_id=previous_claimed_by,
        previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
    )


def ensure_claim_owned(deployment, *, expected_worker_id=None):
    expected_worker_id = expected_worker_id or worker_id()
    claim_state = db.session.execute(
        select(
            PlatformDeployment.claimed_by,
            PlatformDeployment.claimed_at,
            PlatformDeployment.status,
        ).where(PlatformDeployment.id == deployment.id)
    ).one_or_none()
    if claim_state is None:
        raise ClaimLostError(deployment.id, expected_worker_id, None, None, deployment.status)

    current_claimed_by, current_claimed_at, current_status = claim_state
    if current_claimed_by != expected_worker_id or current_claimed_at is None:
        raise ClaimLostError(
            deployment.id,
            expected_worker_id,
            current_claimed_by,
            current_claimed_at,
            current_status,
        )
    return current_claimed_at


def refresh_claim(deployment, *, expected_worker_id=None):
    expected_worker_id = expected_worker_id or worker_id()
    previous_claimed_at = ensure_claim_owned(deployment, expected_worker_id=expected_worker_id)
    new_claimed_at = now_utc()
    updated = db.session.execute(
        update(PlatformDeployment)
        .where(
            and_(
                PlatformDeployment.id == deployment.id,
                PlatformDeployment.claimed_by == expected_worker_id,
                PlatformDeployment.claimed_at.is_not(None),
            )
        )
        .values(claimed_at=new_claimed_at)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not updated:
        raise ClaimLostError(deployment.id, expected_worker_id, None, None, deployment.status)
    deployment.claimed_at = new_claimed_at
    log_claim(
        "refreshed",
        deployment.id,
        worker_id=expected_worker_id,
        previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
        new_claimed_at=new_claimed_at.isoformat(),
    )
    return new_claimed_at


def persist_claim_loss(deployment_id, error):
    db.session.rollback()
    deployment = db.session.get(PlatformDeployment, deployment_id)
    if deployment is None:
        return None

    log_claim(
        "lost",
        deployment_id,
        worker_id=error.worker_id,
        current_claimed_by=error.current_claimed_by,
        current_claimed_at=error.current_claimed_at.isoformat() if error.current_claimed_at else None,
    )
    write_claim_event(
        deployment,
        "claim_lost",
        str(error),
        level="warning",
        metadata=error.metadata,
    )
    db.session.commit()
    return deployment


def claim_heartbeat(deployment, *, expected_worker_id=None):
    refresh_claim(deployment, expected_worker_id=expected_worker_id)
    db.session.commit()


def attach_claim_heartbeat(executor, deployment, *, expected_worker_id=None):
    def heartbeat():
        claim_heartbeat(deployment, expected_worker_id=expected_worker_id)

    setter = getattr(executor, "set_heartbeat", None)
    if callable(setter):
        setter(heartbeat)
    else:
        setattr(executor, "heartbeat", heartbeat)


def mark_failed(deployment, step, message, *, metadata=None):
    metadata = {"summary": message, **(metadata or {})}
    if step == "image.push":
        metadata = push_event_metadata(
            type("PushFailureResult", (), {"metadata": metadata, "log_path": deployment.build.log_path})(),
            message,
            status="failed",
        )
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    secret_values = deployment_secret_values(deployment)
    sanitized_message = redact_text(message, secret_values=secret_values)
    sanitized_metadata = redact_sensitive_data(metadata, secret_values=secret_values)
    deployment.status = "failed"
    deployment.finished_at = now_utc()
    deployment.last_error = sanitized_message
    deployment.build.status = "failed"
    deployment.build.finished_at = now_utc()
    deployment.build.last_error = sanitized_message
    record_event(
        deployment,
        f"{step}.failed",
        "failed",
        sanitized_message,
        step=step,
        level="error",
        metadata={"log_path": deployment.build.log_path, **sanitized_metadata},
    )
    record_event(
        deployment,
        "deployment.failed",
        "failed",
        sanitized_message,
        step="deployment",
        level="error",
        metadata={"log_path": deployment.build.log_path, **sanitized_metadata},
    )
    write_claim_event(
        deployment,
        "claim_cleared",
        "Worker cleared deployment claim after failure",
        metadata={"worker_id": worker_id()},
    )
    release_deployment_claim(deployment)
    db.session.commit()
    return deployment


def claim_next_pending_deployment(*, worker_id=None, claim_ttl_seconds=None):
    worker_name = worker_id or current_app.config.get("CONTROL_PLANE_WORKER_ID", "worker")
    claim_ttl_seconds = (
        current_app.config.get("CONTROL_PLANE_CLAIM_TTL_SECONDS", 300)
        if claim_ttl_seconds is None
        else claim_ttl_seconds
    )
    stale_before = now_utc() - timedelta(seconds=claim_ttl_seconds)

    candidate_rows = list(
        db.session.execute(
            select(
                PlatformDeployment.id,
                PlatformDeployment.claimed_at,
                PlatformDeployment.claimed_by,
            )
            .where(PlatformDeployment.status == "pending")
            .order_by(PlatformDeployment.created_at.asc())
        )
    )

    claimed_at = now_utc()
    for deployment_id, previous_claimed_at, previous_claimed_by in candidate_rows:
        claimed_count = db.session.execute(
            update(PlatformDeployment)
            .where(
                and_(
                    PlatformDeployment.id == deployment_id,
                    PlatformDeployment.status == "pending",
                    or_(
                        PlatformDeployment.claimed_at.is_(None),
                        PlatformDeployment.claimed_at < stale_before,
                    ),
                )
            )
            .values(claimed_at=claimed_at, claimed_by=worker_name)
            .execution_options(synchronize_session=False)
        ).rowcount
        if claimed_count:
            db.session.commit()
            deployment = db.session.get(PlatformDeployment, deployment_id)
            metadata = {
                "worker_id": worker_name,
                "claimed_at": claimed_at.isoformat(),
                "previous_claimed_by": previous_claimed_by,
                "previous_claimed_at": previous_claimed_at.isoformat() if previous_claimed_at else None,
                "reclaimed_stale": previous_claimed_at is not None,
            }
            write_claim_event(
                deployment,
                "claim_acquired",
                "Worker claimed pending deployment",
                metadata=metadata,
            )
            db.session.commit()
            log_claim(
                "acquired",
                deployment_id,
                worker_id=worker_name,
                previous_claimed_by=previous_claimed_by,
                previous_claimed_at=previous_claimed_at.isoformat() if previous_claimed_at else None,
                new_claimed_at=claimed_at.isoformat(),
                reclaimed_stale=previous_claimed_at is not None,
            )
            return deployment

    db.session.rollback()
    return None


def begin_step(deployment, status, *, event_type, message):
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    set_deployment_status(deployment, status, event_type=event_type, message=message)
    db.session.commit()


def commit_step_result(deployment, *, event_type, status, message, step, metadata, extra_events=None):
    ensure_claim_owned(deployment)
    refresh_claim(deployment)
    record_auxiliary_events(deployment, extra_events)
    record_event(
        deployment,
        event_type,
        status,
        message,
        step=step,
        metadata=metadata,
    )
    db.session.commit()


def process_deployment(deployment, executor=None):
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
        deployment.build.status = "succeeded"
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

        begin_step(
            deployment,
            "deploying",
            event_type="deployment.apply_started",
            message="Worker started deployment apply step",
        )
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
        deployment.status = "running"
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
    except WorkerExecutionError as exc:
        if exc.log_path:
            deployment.build.log_path = exc.log_path
        if exc.step == "image.push":
            deployment.build.registry_push_status = "failed"
        persist_preflight_failure(deployment, exc)
        try:
            record_auxiliary_events(deployment, exc.events)
            return mark_failed(deployment, exc.step, exc.message, metadata=exc.metadata)
        except ClaimLostError as claim_exc:
            return persist_claim_loss(deployment.id, claim_exc)


def process_next_pending_deployment(executor=None):
    deployment = claim_next_pending_deployment()
    if deployment is None:
        return None

    return process_deployment(deployment, executor=executor)
