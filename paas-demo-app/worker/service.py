from backend.extensions import db
from backend.models import DeploymentEvent, PlatformDeployment
from worker.executor import FakeDeploymentExecutor, WorkerExecutionError


BUILD_STATUS_BY_DEPLOYMENT_STATUS = {
    "cloning": "cloning",
    "building": "building",
    "testing": "testing",
    "pushing_image": "pushing_image",
}


def record_event(deployment, event_type, status, message):
    db.session.add(
        DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            status=status,
            message=message,
        )
    )


def set_deployment_status(deployment, status, *, event_type, message):
    deployment.status = status
    build_status = BUILD_STATUS_BY_DEPLOYMENT_STATUS.get(status)
    if build_status:
        deployment.build.status = build_status
    record_event(deployment, event_type, status, message)
    db.session.flush()


def mark_failed(deployment, step, message):
    deployment.status = "failed"
    deployment.build.status = "failed"
    record_event(deployment, f"{step}.failed", "failed", message)
    record_event(deployment, "deployment.failed", "failed", message)
    db.session.commit()
    return deployment


def get_next_pending_deployment():
    return (
        PlatformDeployment.query.filter_by(status="pending")
        .order_by(PlatformDeployment.created_at.asc())
        .first()
    )


def process_deployment(deployment, executor=None):
    executor = executor or FakeDeploymentExecutor()

    try:
        set_deployment_status(
            deployment,
            "cloning",
            event_type="repository.clone_started",
            message="Worker started cloning repository",
        )
        executor.clone_repository(deployment)

        set_deployment_status(
            deployment,
            "building",
            event_type="image.build_started",
            message="Worker started building image",
        )
        executor.build_image(deployment)

        if deployment.build.test_command:
            set_deployment_status(
                deployment,
                "testing",
                event_type="tests.started",
                message=f"Worker started tests with '{deployment.build.test_command}'",
            )
            executor.run_tests(deployment)
            record_event(deployment, "tests.succeeded", "testing", "Tests completed successfully")

        set_deployment_status(
            deployment,
            "pushing_image",
            event_type="image.push_started",
            message="Worker started pushing image",
        )
        executor.push_image(deployment)
        deployment.build.status = "succeeded"
        record_event(deployment, "image.push_succeeded", "succeeded", "Image pushed successfully")

        set_deployment_status(
            deployment,
            "deploying",
            event_type="kubernetes.apply_started",
            message="Worker started Kubernetes deployment",
        )
        deployment.service_url = executor.deploy_application(deployment)
        record_event(deployment, "rollout.started", "deploying", "Kubernetes rollout started")

        deployment.status = "running"
        record_event(deployment, "deployment.running", "running", "Deployment is now running")
        db.session.commit()
        return deployment
    except WorkerExecutionError as exc:
        return mark_failed(deployment, exc.step, exc.message)


def process_next_pending_deployment(executor=None):
    deployment = get_next_pending_deployment()
    if deployment is None:
        return None

    return process_deployment(deployment, executor=executor)
