from control_plane.extensions import db
from control_plane.deployment_spec import project_for_deployment
from control_plane.models import DeploymentEvent
from control_plane.security import redact_sensitive_data, redact_text, secret_values_from_env_vars


def deployment_secret_values(deployment):
    return secret_values_from_env_vars(project_for_deployment(deployment).env_vars if deployment else [])


def record_event(deployment, event_type, status, message, *, step=None, level="info", metadata=None):
    secret_values = deployment_secret_values(deployment)
    event_metadata = dict(metadata or {})
    if deployment.origin_request_id:
        event_metadata.setdefault("origin_request_id", deployment.origin_request_id)
    db.session.add(
        DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            step=step,
            level=level,
            status=status,
            message=redact_text(message, secret_values=secret_values) if message else None,
            metadata_json=redact_sensitive_data(event_metadata, secret_values=secret_values),
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
