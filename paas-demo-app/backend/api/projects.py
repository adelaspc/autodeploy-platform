from datetime import datetime, timezone
from collections import deque
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from flask import Blueprint, abort, current_app, jsonify, request

from backend.extensions import db
from backend.models import Build, DeploymentEvent, PlatformDeployment, Project
from worker.executor import WorkerExecutionError, create_executor_for_deployment


projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")


def get_project_or_404(project_id):
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    return project


def get_project_deployment_or_404(project_id, deployment_id):
    deployment = PlatformDeployment.query.filter_by(id=deployment_id, project_id=project_id).first()
    if deployment is None:
        abort(404)
    return deployment


def validate_project_payload(payload):
    required_fields = ("name", "repo_url", "branch", "port", "healthcheck_path")
    missing_fields = [field for field in required_fields if payload.get(field) in (None, "")]
    if missing_fields:
        return f"Missing required fields: {', '.join(missing_fields)}"

    trigger = payload.get("trigger", "manual")
    if trigger not in Project.VALID_TRIGGERS:
        return "Invalid trigger. Expected one of: " + ", ".join(Project.VALID_TRIGGERS)

    runtime = payload.get("runtime", "dockerfile")
    if runtime not in Project.VALID_RUNTIMES:
        return "Invalid runtime. Expected one of: " + ", ".join(Project.VALID_RUNTIMES)

    port = payload.get("port")
    if not isinstance(port, int) or port < 1 or port > 65535:
        return "Invalid port. Expected an integer between 1 and 65535"

    env_vars = payload.get("env_vars", [])
    if not isinstance(env_vars, list):
        return "Invalid env_vars. Expected a list of environment variable definitions"

    return None


def validate_project_patch_payload(payload):
    allowed_fields = {
        "name",
        "repo_url",
        "branch",
        "dockerfile_path",
        "build_context",
        "port",
        "healthcheck_path",
        "env_vars",
        "migration_command",
        "cpu",
        "memory",
        "trigger",
        "runtime",
    }
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}
    if not update_data:
        return None, "Provide at least one updatable field"

    if "trigger" in update_data and update_data["trigger"] not in Project.VALID_TRIGGERS:
        return None, "Invalid trigger. Expected one of: " + ", ".join(Project.VALID_TRIGGERS)

    if "runtime" in update_data and update_data["runtime"] not in Project.VALID_RUNTIMES:
        return None, "Invalid runtime. Expected one of: " + ", ".join(Project.VALID_RUNTIMES)

    if "port" in update_data:
        port = update_data["port"]
        if not isinstance(port, int) or port < 1 or port > 65535:
            return None, "Invalid port. Expected an integer between 1 and 65535"

    if "env_vars" in update_data and not isinstance(update_data["env_vars"], list):
        return None, "Invalid env_vars. Expected a list of environment variable definitions"

    return update_data, None


def validate_deployment_request_payload(payload):
    if not payload.get("commit_sha"):
        return "Missing required field: commit_sha"

    status = payload.get("status", "pending")
    if status not in PlatformDeployment.VALID_STATUSES:
        return "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)

    build_status = payload.get("build_status", "pending")
    if build_status not in Build.VALID_STATUSES:
        return "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    return None


def validate_deployment_patch_payload(payload, deployment):
    allowed_fields = {"status", "service_url", "build_status", "message"}
    update_data = {key: value for key, value in payload.items() if key in allowed_fields}
    if not update_data:
        return None, "Provide at least one updatable field: status, service_url, build_status"

    next_status = update_data.get("status")
    if next_status:
        if next_status not in PlatformDeployment.VALID_STATUSES:
            return None, "Invalid deployment status. Expected one of: " + ", ".join(PlatformDeployment.VALID_STATUSES)
        if not deployment.can_transition_to(next_status):
            return None, (
                f"Invalid deployment transition from {deployment.status} to {next_status}. "
                f"Allowed transitions: {', '.join(deployment.allowed_transitions) or 'none'}"
            )

    build_status = update_data.get("build_status")
    if build_status and build_status not in Build.VALID_STATUSES:
        return None, "Invalid build status. Expected one of: " + ", ".join(Build.VALID_STATUSES)

    return update_data, None


def create_deployment_event(
    deployment_id,
    event_type,
    status,
    message=None,
    *,
    step=None,
    level="info",
    metadata_json=None,
):
    event = DeploymentEvent(
        deployment_id=deployment_id,
        event_type=event_type,
        step=step,
        level=level,
        status=status,
        message=message,
        metadata_json=metadata_json,
    )
    db.session.add(event)


def now_utc():
    return datetime.now(timezone.utc)


def get_runtime_log_path(deployment):
    for event in sorted(deployment.events, key=lambda item: item.created_at, reverse=True):
        metadata = event.metadata_json or {}
        runtime_log_path = metadata.get("runtime_log_path")
        if runtime_log_path:
            return Path(runtime_log_path)
    return None


def is_safe_log_path(path):
    allowed_roots = [
        Path(current).resolve()
        for current in {
            current_app.config["CONTROL_PLANE_WORKSPACE_ROOT"],
            current_app.instance_path,
        }
    ]
    resolved = path.resolve()
    return any(root == resolved or root in resolved.parents for root in allowed_roots)


def read_log_tail(path, *, tail_lines):
    lines = deque(maxlen=tail_lines)
    total_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            total_lines += 1
            lines.append(line.rstrip("\n"))
    return {
        "content": "\n".join(lines),
        "line_count": total_lines,
        "truncated": total_lines > tail_lines,
    }


def stop_deployment_runtime(deployment, *, message=None):
    executor = create_executor_for_deployment(deployment)
    create_deployment_event(
        deployment.id,
        "deployment.stop_started",
        "stopped",
        message or "Stopping deployment runtime",
        step="deploy.stop",
    )
    stop_result = executor.stop(deployment)
    deployment.status = "stopped"
    deployment.service_url = None
    deployment.finished_at = now_utc()
    if stop_result.deploy_target:
        deployment.deploy_target = stop_result.deploy_target
    if stop_result.container_name:
        deployment.container_name = stop_result.container_name
    if stop_result.container_id:
        deployment.container_id = stop_result.container_id
    if stop_result.host_port is not None:
        deployment.host_port = stop_result.host_port
    if stop_result.healthcheck_url:
        deployment.healthcheck_url = stop_result.healthcheck_url
    create_deployment_event(
        deployment.id,
        "deployment.stopped",
        "stopped",
        stop_result.message,
        step="deploy.stop",
        metadata_json=stop_result.metadata
        | {
            "log_path": stop_result.log_path,
            "summary": stop_result.message,
            "container_name": deployment.container_name,
            "container_id": deployment.container_id,
        },
    )


@projects_bp.get("")
def list_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([project.to_dict() for project in projects])


@projects_bp.post("")
def create_project():
    payload = request.get_json(silent=True) or {}
    validation_error = validate_project_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    project = Project(
        name=payload["name"],
        repo_url=payload["repo_url"],
        branch=payload["branch"],
        dockerfile_path=payload.get("dockerfile_path", "Dockerfile"),
        build_context=payload.get("build_context", "."),
        port=payload["port"],
        healthcheck_path=payload["healthcheck_path"],
        env_vars=payload.get("env_vars", []),
        migration_command=payload.get("migration_command"),
        cpu=payload.get("cpu"),
        memory=payload.get("memory"),
        trigger=payload.get("trigger", "manual"),
        runtime=payload.get("runtime", "dockerfile"),
    )
    try:
        db.session.add(project)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Project name must be unique"}), 409

    return jsonify(project.to_dict()), 201


@projects_bp.get("/<int:project_id>")
def get_project(project_id):
    return jsonify(get_project_or_404(project_id).to_dict())


@projects_bp.patch("/<int:project_id>")
def update_project(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_project_patch_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    for key, value in update_data.items():
        setattr(project, key, value)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Project name must be unique"}), 409

    return jsonify(project.to_dict())


@projects_bp.delete("/<int:project_id>")
def delete_project(project_id):
    project = get_project_or_404(project_id)
    db.session.delete(project)
    db.session.commit()
    return jsonify({"message": "Project deleted"}), 200


@projects_bp.get("/<int:project_id>/builds")
def list_project_builds(project_id):
    project = get_project_or_404(project_id)
    builds = Build.query.filter_by(project_id=project.id).order_by(Build.created_at.desc()).all()
    return jsonify([build.to_dict() for build in builds])


@projects_bp.get("/<int:project_id>/deployments")
def list_project_deployments(project_id):
    project = get_project_or_404(project_id)
    deployments = PlatformDeployment.query.filter_by(project_id=project.id).order_by(
        PlatformDeployment.created_at.desc()
    ).all()
    return jsonify(
        [
            {
                **deployment.to_dict(),
                "build": deployment.build.to_dict(),
            }
            for deployment in deployments
        ]
    )


@projects_bp.post("/<int:project_id>/deployments")
def create_project_deployment(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    validation_error = validate_deployment_request_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    registry = payload.get("registry")
    image_name = payload.get("image_name")
    image_tag = payload.get("image_tag")
    image_ref = payload.get("image_ref")
    if not image_ref and registry and image_name and image_tag:
        image_ref = f"{registry.rstrip('/')}/{image_name}:{image_tag}"

    build = Build(
        project_id=project.id,
        commit_sha=payload["commit_sha"],
        registry=registry,
        image_name=image_name,
        image_tag=image_tag,
        image_ref=image_ref,
        status=payload.get("build_status", "pending"),
        test_command=payload.get("test_command"),
    )
    db.session.add(build)
    db.session.flush()

    deployment = PlatformDeployment(
        project_id=project.id,
        build_id=build.id,
        environment=payload.get("environment", "production"),
        status=payload.get("status", "pending"),
        service_url=payload.get("service_url"),
    )
    db.session.add(deployment)
    db.session.flush()

    create_deployment_event(
        deployment.id,
        "deployment.created",
        deployment.status,
        payload.get("message", "Deployment record created"),
        step="deployment",
    )

    if build.status != "pending":
        create_deployment_event(
            deployment.id,
            "build.status_reported",
            build.status,
            f"Build recorded in status '{build.status}'",
            step="build",
        )

    db.session.commit()

    return (
        jsonify(
            {
                **deployment.to_dict(),
                "build": build.to_dict(),
                "events": [event.to_dict() for event in deployment.events],
            }
        ),
        201,
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>")
def get_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    return jsonify(
        {
            **deployment.to_dict(),
            "build": deployment.build.to_dict(),
            "events": [event.to_dict() for event in deployment.events],
        }
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/runtime-log")
def get_project_deployment_runtime_log(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    runtime_log_path = get_runtime_log_path(deployment)
    if runtime_log_path is None:
        return jsonify({"error": "No runtime log is available for this deployment"}), 404
    if not is_safe_log_path(runtime_log_path):
        return jsonify({"error": "Runtime log path is outside allowed log roots"}), 409
    if not runtime_log_path.is_file():
        return jsonify({"error": "Runtime log file does not exist", "path": str(runtime_log_path)}), 404

    tail_lines = request.args.get("tail_lines", default=200, type=int)
    if tail_lines is None or tail_lines < 1 or tail_lines > 2000:
        return jsonify({"error": "tail_lines must be an integer between 1 and 2000"}), 400

    payload = read_log_tail(runtime_log_path, tail_lines=tail_lines)
    return jsonify(
        {
            "deployment_id": deployment.id,
            "path": str(runtime_log_path),
            "tail_lines": tail_lines,
            **payload,
        }
    )


@projects_bp.patch("/<int:project_id>/deployments/<int:deployment_id>")
def update_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_deployment_patch_payload(payload, deployment)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    message = update_data.get("message")

    if update_data.get("status") == "stopped":
        try:
            stop_deployment_runtime(deployment, message=message)
        except WorkerExecutionError as exc:
            db.session.rollback()
            return (
                jsonify(
                    {
                        "error": exc.message,
                        "step": exc.step,
                        "metadata": exc.metadata,
                    }
                ),
                409,
            )
    elif "status" in update_data:
        deployment.status = update_data["status"]
        create_deployment_event(
            deployment.id,
            "deployment.status_updated",
            deployment.status,
            message or f"Deployment status updated to '{deployment.status}'",
            step="deployment",
        )

    if "service_url" in update_data:
        deployment.service_url = update_data["service_url"]
        create_deployment_event(
            deployment.id,
            "deployment.service_url_updated",
            deployment.status,
            message or f"Service URL updated to '{deployment.service_url}'",
            step="deploy",
        )

    if "build_status" in update_data:
        deployment.build.status = update_data["build_status"]
        create_deployment_event(
            deployment.id,
            "build.status_updated",
            deployment.build.status,
            message or f"Build status updated to '{deployment.build.status}'",
            step="build",
        )

    db.session.commit()

    return jsonify(
        {
            **deployment.to_dict(),
            "build": deployment.build.to_dict(),
            "events": [event.to_dict() for event in deployment.events],
        }
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/events")
def list_project_deployment_events(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    events = DeploymentEvent.query.filter_by(deployment_id=deployment.id).order_by(DeploymentEvent.created_at.asc()).all()
    return jsonify([event.to_dict() for event in events])
