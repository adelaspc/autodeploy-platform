import hashlib
import hmac
from urllib.parse import urlparse

from control_plane.application.deployments.orchestration import create_requested_deployment
from control_plane.application.projects.service import serialize_triggered_deployment
from control_plane.api.request_context import error_payload
from sqlalchemy.exc import IntegrityError
from flask import Blueprint, current_app, jsonify, request

from control_plane.application.projects.validation import kubernetes_deployment_prereq_error
from control_plane.extensions import db
from control_plane.models import Project, WebhookDelivery


webhooks_bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")


def normalize_github_repository_url(value):
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    if candidate.startswith("git@github.com:"):
        candidate = f"https://github.com/{candidate[len('git@github.com:') :]}"
    elif candidate.startswith("ssh://git@github.com/"):
        candidate = f"https://github.com/{candidate[len('ssh://git@github.com/') :]}"

    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com":
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"https://github.com{path.lower()}"

    return candidate.rstrip("/")


def github_webhook_secret():
    secret = current_app.config.get("CONTROL_PLANE_GITHUB_WEBHOOK_SECRET")
    return secret if isinstance(secret, str) and secret else None


def verify_github_signature(payload, signature_header):
    secret = github_webhook_secret()
    if secret is None:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    provided_signature = signature_header.split("=", 1)[1]
    expected_signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_signature, provided_signature)


def github_branch_from_ref(ref):
    prefix = "refs/heads/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    return ref[len(prefix) :]


def github_push_deletes_branch(payload, commit_sha):
    if payload.get("deleted") is True:
        return True
    return isinstance(commit_sha, str) and bool(commit_sha) and set(commit_sha) == {"0"}


def base_github_response(*, delivery_id, event_type, repository_url=None, branch=None, commit_sha=None):
    return {
        "event": event_type or None,
        "delivery_id": delivery_id,
        "repository_url": repository_url,
        "branch": branch,
        "commit_sha": commit_sha,
    }


def ignore_github_event(reason, *, delivery_id, event_type, repository_url=None, branch=None, commit_sha=None):
    current_app.logger.info(
        "Ignoring GitHub webhook",
        extra={
            "delivery_id": delivery_id,
            "event_type": event_type,
            "reason": reason,
            "repository_url": repository_url,
            "branch": branch,
            "commit_sha": commit_sha,
        },
    )
    return (
        jsonify(
            {
                "status": "ignored",
                "reason": reason,
                **base_github_response(
                    delivery_id=delivery_id,
                    event_type=event_type,
                    repository_url=repository_url,
                    branch=branch,
                    commit_sha=commit_sha,
                ),
            }
        ),
        202,
    )


def reserve_delivery_id(delivery_id, *, event_type, repository_url=None, branch=None, commit_sha=None):
    if not delivery_id:
        return None, False

    existing = WebhookDelivery.query.filter_by(delivery_id=delivery_id).first()
    if existing is not None:
        return existing, True

    delivery = WebhookDelivery(
        delivery_id=delivery_id,
        event_type=event_type or "unknown",
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
        status="received",
    )
    db.session.add(delivery)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existing = WebhookDelivery.query.filter_by(delivery_id=delivery_id).first()
        return existing, True

    return delivery, False


def persist_ignored_delivery(
    *,
    delivery_id,
    event_type,
    reason,
    repository_url=None,
    branch=None,
    commit_sha=None,
):
    delivery, is_duplicate = reserve_delivery_id(
        delivery_id,
        event_type=event_type,
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
    )
    if is_duplicate:
        return ignore_github_event(
            "duplicate_delivery",
            delivery_id=delivery_id,
            event_type=event_type,
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    if delivery is not None:
        delivery.status = "ignored"
        delivery.reason = reason
        db.session.commit()

    return ignore_github_event(
        reason,
        delivery_id=delivery_id,
        event_type=event_type,
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
    )


def github_push_project_matches(repository_url, branch):
    normalized_repository_url = normalize_github_repository_url(repository_url)
    if not normalized_repository_url:
        return [], []

    projects = Project.query.filter_by(trigger="github_push").all()
    repository_matches = [
        project
        for project in projects
        if normalize_github_repository_url(project.repo_url) == normalized_repository_url
    ]
    branch_matches = [project for project in repository_matches if project.branch == branch]
    return repository_matches, branch_matches


@webhooks_bp.post("/github")
def github_webhook():
    payload_bytes = request.get_data(cache=True)
    signature_header = request.headers.get("X-Hub-Signature-256")
    if not verify_github_signature(payload_bytes, signature_header):
        return jsonify(error_payload("Invalid GitHub webhook signature")), 401

    event_type = request.headers.get("X-GitHub-Event", "").strip().lower()
    delivery_id = request.headers.get("X-GitHub-Delivery")
    payload = request.get_json(silent=True) or {}
    repository = payload.get("repository") or {}
    repository_url = repository.get("clone_url") or repository.get("html_url") or repository.get("ssh_url")
    branch = github_branch_from_ref(payload.get("ref"))
    commit_sha = payload.get("after")

    if event_type == "ping":
        current_app.logger.info("Accepted GitHub ping webhook", extra={"delivery_id": delivery_id})
        return jsonify({"message": "GitHub webhook ping received", "event": "ping", "delivery_id": delivery_id}), 200

    if event_type != "push":
        return persist_ignored_delivery(
            delivery_id=delivery_id,
            event_type=event_type or "unknown",
            reason="unsupported_event_type",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    if not repository_url or not branch or not commit_sha:
        return persist_ignored_delivery(
            delivery_id=delivery_id,
            event_type="push",
            reason="unsupported_ref",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    if github_push_deletes_branch(payload, commit_sha):
        return persist_ignored_delivery(
            delivery_id=delivery_id,
            event_type="push",
            reason="branch_deleted",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    repository_matches, branch_matches = github_push_project_matches(repository_url, branch)
    if not repository_matches:
        return persist_ignored_delivery(
            delivery_id=delivery_id,
            event_type="push",
            reason="unmatched_repository",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    if not branch_matches:
        return persist_ignored_delivery(
            delivery_id=delivery_id,
            event_type="push",
            reason="branch_mismatch",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    delivery, is_duplicate = reserve_delivery_id(
        delivery_id,
        event_type="push",
        repository_url=repository_url,
        branch=branch,
        commit_sha=commit_sha,
    )
    if is_duplicate:
        return ignore_github_event(
            "duplicate_delivery",
            delivery_id=delivery_id,
            event_type="push",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    deployment_prereq_error = kubernetes_deployment_prereq_error()
    if deployment_prereq_error:
        if delivery is not None:
            delivery.status = "ignored"
            delivery.reason = "platform_not_ready"
            db.session.commit()
        return ignore_github_event(
            "platform_not_ready",
            delivery_id=delivery_id,
            event_type="push",
            repository_url=repository_url,
            branch=branch,
            commit_sha=commit_sha,
        )

    deployments = []
    for project in branch_matches:
        _build, deployment, _commit_sha = create_requested_deployment(
            project,
            branch=branch,
            test_command=project.default_test_command,
            commit_sha=commit_sha,
            message_prefix="Deployment requested from GitHub webhook",
            deployment_metadata={
                "trigger": "github_push",
                "source": "github_webhook",
                "event_type": "push",
                "github_delivery_id": delivery_id,
            },
            extra_events=[
                {
                    "event_type": "webhook.github_push_received",
                    "status": "pending",
                    "message": f"GitHub push webhook accepted for branch '{branch}'",
                    "step": "webhook",
                    "metadata_json": {
                        "event_type": "push",
                        "branch": branch,
                        "commit_sha": commit_sha,
                        "github_delivery_id": delivery_id,
                        "repository_url": repository_url,
                    },
                }
            ],
            commit=False,
        )
        deployments.append(
            serialize_triggered_deployment(
                deployment,
                branch=branch,
            )
        )

    if delivery is not None:
        delivery.status = "accepted"
        delivery.reason = None
        if len(deployments) == 1:
            delivery.deployment_id = deployments[0]["deployment_id"]

    db.session.commit()

    current_app.logger.info(
        "Accepted GitHub push webhook",
        extra={
            "delivery_id": delivery_id,
            "event_type": "push",
            "repository_url": repository_url,
            "branch": branch,
            "commit_sha": commit_sha,
            "deployment_ids": [item["deployment_id"] for item in deployments],
        },
    )
    return (
        jsonify(
            {
                "status": "accepted",
                "message": "GitHub push webhook accepted",
                **base_github_response(
                    delivery_id=delivery_id,
                    event_type="push",
                    repository_url=repository_url,
                    branch=branch,
                    commit_sha=commit_sha,
                ),
                "deployments": deployments,
            }
        ),
        202,
    )
