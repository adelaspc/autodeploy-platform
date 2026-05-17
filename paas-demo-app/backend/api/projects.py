from flask import Blueprint, jsonify, request

from backend.api.audit_service import record_audit_event
from backend.api.auth import authorize_request, require_api_role
from backend.api.deployment_orchestration import create_user_facing_deployment, get_deployment_branch, get_latest_project_deployment_record
from backend.api.deployment_queries import get_project_deployment_or_404, get_project_or_404
from backend.api.deployment_services import apply_deployment_update, create_manual_deployment
from backend.api.request_parsing import parse_bool_arg, parse_limit_arg, parse_optional_int_arg, parse_status_filter_arg
from backend.api.project_validation import (
    kubernetes_deployment_prereq_error,
    normalize_project_spec_fields,
    resolve_effective_test_command,
    validate_deployment_patch_payload,
    validate_deployment_request_payload,
    validate_project_deploy_payload,
    validate_project_patch_payload,
    validate_project_payload,
)
from backend.api.project_services import (
    ProjectConflictError,
    create_project_record,
    list_project_deployments_query,
    serialize_latest_project_deployment,
    serialize_project_deployments_page,
    serialize_project_deployment,
    serialize_triggered_deployment,
    update_project_record,
)
from backend.security import redact_sensitive_data, secret_values_from_env_vars
from backend.api.deployment_read_models import (
    get_build_log_path,
    get_runtime_log_path,
    is_safe_log_path,
    serialize_build_log_payload,
    serialize_runtime_log_payload,
    serialize_deployment_summary,
    serialize_kubernetes_diagnostics,
)
from backend.api.platform_read_models import project_activity_payload, project_status_payload
from backend.extensions import db
from backend.models import Build, DeploymentEvent, PlatformDeployment, Project
from worker.executor import WorkerExecutionError


projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")


@projects_bp.before_request
def protect_projects_api():
    return authorize_request("read_only")


@projects_bp.get("")
def list_projects():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return jsonify([project.to_dict() for project in projects])


@projects_bp.post("")
@require_api_role("admin")
def create_project():
    payload = request.get_json(silent=True) or {}
    validation_error = validate_project_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    payload = normalize_project_spec_fields(payload)

    try:
        project = create_project_record(payload)
    except ProjectConflictError:
        return jsonify({"error": "Project name must be unique"}), 409
    record_audit_event(
        action="project.created",
        resource_type="project",
        resource_id=project.id,
        metadata={
            "name": project.name,
            "branch": project.branch,
            "trigger": project.trigger,
            "runtime": project.runtime,
            "repo_url": project.repo_url,
        },
    )

    return jsonify(project.to_dict()), 201


@projects_bp.get("/<int:project_id>")
def get_project(project_id):
    return jsonify(get_project_or_404(project_id).to_dict())


@projects_bp.get("/<int:project_id>/status")
def get_project_status(project_id):
    project = get_project_or_404(project_id)
    return jsonify(project_status_payload(project))


@projects_bp.get("/<int:project_id>/activity")
def get_project_activity(project_id):
    project = get_project_or_404(project_id)
    latest_limit, error_response, status_code = parse_limit_arg("latest_limit", default=10, request=request)
    if error_response is not None:
        return error_response, status_code
    webhook_limit, error_response, status_code = parse_limit_arg("webhook_limit", default=10, request=request)
    if error_response is not None:
        return error_response, status_code
    deployment_statuses, error_response, status_code = parse_status_filter_arg(
        "deployment_status",
        request=request,
        allowed_values=PlatformDeployment.VALID_STATUSES,
    )
    if error_response is not None:
        return error_response, status_code
    webhook_statuses, error_response, status_code = parse_status_filter_arg(
        "webhook_status",
        request=request,
        allowed_values=("accepted", "ignored"),
    )
    if error_response is not None:
        return error_response, status_code
    active_only, error_response, status_code = parse_bool_arg("active_only", request=request)
    if error_response is not None:
        return error_response, status_code
    include_latest_failed, error_response, status_code = parse_bool_arg("include_latest_failed", request=request)
    if error_response is not None:
        return error_response, status_code
    include_webhooks, error_response, status_code = parse_bool_arg("include_webhooks", request=request)
    if error_response is not None:
        return error_response, status_code
    before_deployment_id, error_response, status_code = parse_optional_int_arg(
        "before_deployment_id",
        request=request,
        min_value=1,
    )
    if error_response is not None:
        return error_response, status_code
    before_webhook_delivery_id, error_response, status_code = parse_optional_int_arg(
        "before_webhook_delivery_id",
        request=request,
        min_value=1,
    )
    if error_response is not None:
        return error_response, status_code
    return jsonify(
        project_activity_payload(
            project,
            latest_limit=latest_limit,
            webhook_limit=webhook_limit,
            deployment_statuses=deployment_statuses,
            webhook_statuses=webhook_statuses,
            active_only=bool(active_only),
            include_latest_failed=True if include_latest_failed is None else include_latest_failed,
            include_webhooks=True if include_webhooks is None else include_webhooks,
            before_deployment_id=before_deployment_id,
            before_webhook_delivery_id=before_webhook_delivery_id,
        )
    )


@projects_bp.patch("/<int:project_id>")
@require_api_role("admin")
def update_project(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_project_patch_payload(payload, project)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    update_data = normalize_project_spec_fields(update_data)

    try:
        update_project_record(project, update_data)
    except ProjectConflictError:
        return jsonify({"error": "Project name must be unique"}), 409
    record_audit_event(
        action="project.updated",
        resource_type="project",
        resource_id=project.id,
        metadata={
            "updated_fields": sorted(update_data.keys()),
        },
    )

    return jsonify(project.to_dict())


@projects_bp.delete("/<int:project_id>")
@require_api_role("admin")
def delete_project(project_id):
    project = get_project_or_404(project_id)
    project_metadata = {
        "name": project.name,
        "branch": project.branch,
        "trigger": project.trigger,
    }
    db.session.delete(project)
    db.session.commit()
    record_audit_event(
        action="project.deleted",
        resource_type="project",
        resource_id=project_id,
        metadata=project_metadata,
    )
    return jsonify({"message": "Project deleted"}), 200


@projects_bp.get("/<int:project_id>/builds")
def list_project_builds(project_id):
    project = get_project_or_404(project_id)
    builds = Build.query.filter_by(project_id=project.id).order_by(Build.created_at.desc()).all()
    return jsonify([build.to_dict() for build in builds])


@projects_bp.get("/<int:project_id>/deployments")
def list_project_deployments(project_id):
    project = get_project_or_404(project_id)
    limit, error_response, status_code = parse_limit_arg("limit", default=20, request=request)
    if error_response is not None:
        return error_response, status_code
    before_deployment_id, error_response, status_code = parse_optional_int_arg(
        "before_deployment_id",
        request=request,
        min_value=1,
    )
    if error_response is not None:
        return error_response, status_code
    deployment_statuses, error_response, status_code = parse_status_filter_arg(
        "deployment_status",
        request=request,
        allowed_values=PlatformDeployment.VALID_STATUSES,
    )
    if error_response is not None:
        return error_response, status_code

    deployments_query = list_project_deployments_query(project.id)
    if before_deployment_id is not None:
        deployments_query = deployments_query.filter(PlatformDeployment.id < before_deployment_id)
    if deployment_statuses:
        deployments_query = deployments_query.filter(PlatformDeployment.status.in_(tuple(deployment_statuses)))
    deployments = deployments_query.limit(limit).all()
    return jsonify(serialize_project_deployments_page(deployments, limit=limit))


@projects_bp.get("/<int:project_id>/deployments/latest")
def get_latest_project_deployment(project_id):
    project = get_project_or_404(project_id)
    deployment = get_latest_project_deployment_record(project.id)
    if deployment is None:
        return jsonify({"error": "Project has no deployments"}), 404

    return jsonify(serialize_latest_project_deployment(deployment, branch=get_deployment_branch(deployment)))


@projects_bp.post("/<int:project_id>/deployments")
@require_api_role("admin")
def create_project_deployment(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    validation_error = validate_deployment_request_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    deployment_prereq_error = kubernetes_deployment_prereq_error()
    if deployment_prereq_error:
        return jsonify({"error": deployment_prereq_error}), 409

    effective_test_command = resolve_effective_test_command(
        project,
        requested_test_command=payload.get("test_command"),
        payload_includes_test_command="test_command" in payload,
    )

    _build, deployment = create_manual_deployment(project, payload, test_command=effective_test_command)
    record_audit_event(
        action="deployment.manual_created",
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={
            "project_id": project.id,
            "commit_sha": deployment.build.commit_sha,
            "deployment_status": deployment.status,
            "build_status": deployment.build.status,
        },
    )

    return (
        jsonify(serialize_project_deployment(deployment, include_events=True)),
        201,
    )


@projects_bp.post("/<int:project_id>/deploy")
@require_api_role("deployer")
def deploy_project(project_id):
    project = get_project_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    validation_error = validate_project_deploy_payload(payload)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    deployment_prereq_error = kubernetes_deployment_prereq_error()
    if deployment_prereq_error:
        return jsonify({"error": deployment_prereq_error}), 409

    branch = (payload.get("branch") or project.branch).strip()
    test_command = resolve_effective_test_command(
        project,
        requested_test_command=payload.get("test_command"),
        payload_includes_test_command="test_command" in payload,
    )

    try:
        build, deployment, commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Deployment requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    record_audit_event(
        action="deployment.deploy_triggered",
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={
            "project_id": project.id,
            "branch": branch,
            "commit_sha": commit_sha,
        },
    )

    return (
        jsonify(serialize_triggered_deployment(deployment, branch=branch)),
        201,
    )


@projects_bp.post("/<int:project_id>/deployments/<int:deployment_id>/retry")
@require_api_role("deployer")
def retry_project_deployment(project_id, deployment_id):
    project = get_project_or_404(project_id)
    original = get_project_deployment_or_404(project_id, deployment_id)
    deployment_prereq_error = kubernetes_deployment_prereq_error()
    if deployment_prereq_error:
        return jsonify({"error": deployment_prereq_error}), 409
    branch = get_deployment_branch(original)
    test_command = original.build.test_command or project.default_test_command

    try:
        build, deployment, commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Deployment retry requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    record_audit_event(
        action="deployment.retry_triggered",
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={
            "project_id": project.id,
            "branch": branch,
            "commit_sha": commit_sha,
            "retried_from_deployment_id": original.id,
        },
    )

    return (
        jsonify(
            serialize_triggered_deployment(
                deployment,
                branch=branch,
                source_deployment_field="retried_from_deployment_id",
                source_deployment_id=original.id,
            )
        ),
        201,
    )


@projects_bp.post("/<int:project_id>/redeploy")
@require_api_role("deployer")
def redeploy_project(project_id):
    project = get_project_or_404(project_id)
    original = get_latest_project_deployment_record(project.id)
    if original is None:
        return jsonify({"error": "Project has no deployments"}), 404
    deployment_prereq_error = kubernetes_deployment_prereq_error()
    if deployment_prereq_error:
        return jsonify({"error": deployment_prereq_error}), 409

    branch = get_deployment_branch(original)
    test_command = original.build.test_command or project.default_test_command

    try:
        build, deployment, _commit_sha = create_user_facing_deployment(
            project,
            branch=branch,
            test_command=test_command,
            message_prefix="Project redeploy requested",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    record_audit_event(
        action="deployment.redeploy_triggered",
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={
            "project_id": project.id,
            "branch": branch,
            "commit_sha": deployment.build.commit_sha,
            "redeployed_from_deployment_id": original.id,
        },
    )

    return (
        jsonify(
            serialize_triggered_deployment(
                deployment,
                branch=branch,
                source_deployment_field="redeployed_from_deployment_id",
                source_deployment_id=original.id,
            )
        ),
        201,
    )


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>")
def get_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    return jsonify(serialize_project_deployment(deployment, include_events=True))


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/summary")
def get_project_deployment_summary(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    return jsonify(serialize_deployment_summary(deployment, branch=get_deployment_branch(deployment)))


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/kubernetes-diagnostics")
def get_project_deployment_kubernetes_diagnostics(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    diagnostics = serialize_kubernetes_diagnostics(deployment)
    if diagnostics is None:
        return jsonify({"error": "Deployment does not use the Kubernetes target"}), 404
    return jsonify(diagnostics)


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

    return jsonify(serialize_runtime_log_payload(deployment, runtime_log_path, tail_lines=tail_lines))


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/build-log")
def get_project_deployment_build_log(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    build_log_path = get_build_log_path(deployment)
    if build_log_path is None:
        return jsonify({"error": "No build log is available for this deployment"}), 404
    if not is_safe_log_path(build_log_path):
        return jsonify({"error": "Build log path is outside allowed log roots"}), 409
    if not build_log_path.is_file():
        return jsonify({"error": "Build log file does not exist", "path": str(build_log_path)}), 404

    tail_lines = request.args.get("tail_lines", default=200, type=int)
    if tail_lines is None or tail_lines < 1 or tail_lines > 2000:
        return jsonify({"error": "tail_lines must be an integer between 1 and 2000"}), 400

    return jsonify(serialize_build_log_payload(deployment, build_log_path, tail_lines=tail_lines))


@projects_bp.patch("/<int:project_id>/deployments/<int:deployment_id>")
@require_api_role("admin")
def update_project_deployment(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    payload = request.get_json(silent=True) or {}
    update_data, validation_error = validate_deployment_patch_payload(payload, deployment)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    if update_data.get("status") == "stopped":
        try:
            apply_deployment_update(deployment, update_data)
        except WorkerExecutionError as exc:
            db.session.rollback()
            secret_values = secret_values_from_env_vars(deployment.project.env_vars if deployment.project else [])
            record_audit_event(
                action="deployment.stop_requested",
                resource_type="deployment",
                resource_id=deployment.id,
                status="failure",
                metadata={
                    "project_id": project_id,
                    "step": exc.step,
                    "error_message": exc.message,
                },
            )
            return (
                jsonify(
                    {
                        "error": exc.message,
                        "step": exc.step,
                        "metadata": redact_sensitive_data(exc.metadata, secret_values=secret_values),
                    }
                ),
                409,
            )
    else:
        apply_deployment_update(deployment, update_data)

    db.session.commit()
    audit_action = "deployment.stopped" if update_data.get("status") == "stopped" else "deployment.patched"
    record_audit_event(
        action=audit_action,
        resource_type="deployment",
        resource_id=deployment.id,
        metadata={
            "project_id": project_id,
            "updated_fields": sorted(update_data.keys()),
            "status": deployment.status,
            "build_status": deployment.build.status,
            "service_url": deployment.service_url,
        },
    )

    return jsonify(serialize_project_deployment(deployment, include_events=True))


@projects_bp.get("/<int:project_id>/deployments/<int:deployment_id>/events")
def list_project_deployment_events(project_id, deployment_id):
    deployment = get_project_deployment_or_404(project_id, deployment_id)
    events = DeploymentEvent.query.filter_by(deployment_id=deployment.id).order_by(DeploymentEvent.created_at.asc()).all()
    return jsonify([event.to_dict() for event in events])
