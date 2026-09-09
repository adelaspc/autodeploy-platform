"""Remove disposable diagnostics without erasing deployment history."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
from flask import current_app

from control_plane.extensions import db
from control_plane.models import AuditEvent, DeploymentEvent, PlatformDeployment


TERMINAL_RETENTION_STATUSES = ("failed", "stopped")
DISPOSABLE_EVENT_PREFIXES = ("claim_", "reconcile.")


def retention_cutoff(*, older_than_days, now=None):
    current = now or datetime.now(timezone.utc)
    return current - timedelta(days=older_than_days)


def old_terminal_deployments(cutoff):
    return (
        PlatformDeployment.query.filter(
            PlatformDeployment.status.in_(TERMINAL_RETENTION_STATUSES),
            PlatformDeployment.updated_at < cutoff,
        )
        .order_by(PlatformDeployment.id.asc())
        .all()
    )


def safe_deployment_workspace(deployment):
    root = Path(current_app.config.get("CONTROL_PLANE_WORKSPACE_ROOT", "/tmp/paas-workspaces")).resolve()
    candidate = (root / f"project-{deployment.project_id}" / f"deployment-{deployment.id}").resolve()
    # Keep cleanup confined to the configured workspace tree even if path
    # construction changes later.
    if root not in candidate.parents:
        return None
    return candidate


def disposable_event_query(cutoff):
    deployment_ids = db.select(PlatformDeployment.id).where(
        PlatformDeployment.status.in_(TERMINAL_RETENTION_STATUSES),
        PlatformDeployment.updated_at < cutoff,
    )
    return DeploymentEvent.query.filter(
        DeploymentEvent.deployment_id.in_(deployment_ids),
        db.or_(*(DeploymentEvent.event_type.startswith(prefix) for prefix in DISPOSABLE_EVENT_PREFIXES)),
    )


def artifact_paths(deployment, metadata_key, *, event_type=None):
    paths = []
    for event in deployment.events:
        if event_type is not None and event.event_type != event_type:
            continue
        value = (event.metadata_json or {}).get(metadata_key)
        if isinstance(value, str) and value:
            paths.append(Path(value))
    return paths


def artifact_exists_in_workspace(paths, workspace):
    for path in paths:
        try:
            candidate = path.resolve()
        except OSError:
            continue
        if (candidate == workspace or workspace in candidate.parents) and candidate.is_file():
            return True
    return False


def cleanup_observability_data(*, older_than_days, apply=False, include_audit_events=False, now=None):
    current = now or datetime.now(timezone.utc)
    cutoff = retention_cutoff(older_than_days=older_than_days, now=current)
    deployments = old_terminal_deployments(cutoff)
    workspaces = [path for deployment in deployments if (path := safe_deployment_workspace(deployment)).exists()]
    disposable_events = disposable_event_query(cutoff).count()
    audit_events = AuditEvent.query.filter(AuditEvent.created_at < cutoff).count() if include_audit_events else 0
    result = {
        "cutoff": cutoff,
        "deployment_candidates": len(deployments),
        "workspace_candidates": len(workspaces),
        "disposable_event_candidates": disposable_events,
        "audit_event_candidates": audit_events,
        "workspaces_removed": 0,
        "disposable_events_removed": 0,
        "audit_events_removed": 0,
        "errors": [],
    }
    # Dry-run is the default because workspace deletion cannot be recovered from
    # the database.
    if not apply:
        return result

    for deployment in deployments:
        workspace = safe_deployment_workspace(deployment)
        if workspace is None or not workspace.exists():
            continue
        removed_artifacts = []
        build_paths = [
            Path(value)
            for value in (deployment.build.build_log_path, deployment.build.log_path)
            if value
        ] + artifact_paths(deployment, "log_path", event_type="image.build_succeeded")
        if artifact_exists_in_workspace(build_paths, workspace):
            removed_artifacts.append("build_log")
        if artifact_exists_in_workspace(artifact_paths(deployment, "runtime_log_path"), workspace):
            removed_artifacts.append("runtime_log")
        try:
            shutil.rmtree(workspace)
        except OSError as exc:
            result["errors"].append(f"deployment {deployment.id}: {type(exc).__name__}")
            continue
        result["workspaces_removed"] += 1
        build = deployment.build
        build.workspace_path = None
        build.log_path = None
        build.build_log_path = None
        db.session.add(
            DeploymentEvent(
                deployment=deployment,
                event_type="observability.artifacts_removed",
                step="observability.retention",
                level="info",
                status=deployment.status,
                message="Removed workspace artifacts under the explicit retention policy",
                metadata_json={
                    "removed_artifacts": removed_artifacts,
                    "retention_cutoff": cutoff.isoformat(),
                },
                created_at=current,
            )
        )

    result["disposable_events_removed"] = disposable_event_query(cutoff).delete(synchronize_session=False)
    if include_audit_events:
        result["audit_events_removed"] = AuditEvent.query.filter(AuditEvent.created_at < cutoff).delete(
            synchronize_session=False
        )
    db.session.commit()
    return result


def _summary(result, *, apply):
    mode = "APPLY" if apply else "DRY-RUN"
    return (
        f"{mode}: {result['workspace_candidates']} workspace(s), "
        f"{result['disposable_event_candidates']} disposable event(s), "
        f"{result['audit_event_candidates']} audit event(s) before {result['cutoff'].isoformat()}"
    )


@click.command("cleanup-observability")
@click.option("--older-than-days", type=click.IntRange(min=1), required=True)
@click.option("--apply", is_flag=True, help="Apply deletion. Without this flag the command is a dry-run.")
@click.option(
    "--include-audit-events",
    is_flag=True,
    help="Also remove audit events older than the cutoff. Deployment lifecycle and webhook records are retained.",
)
def cleanup_observability(older_than_days, apply, include_audit_events):
    """Remove old observability artifacts while preserving deployment lifecycle history."""
    result = cleanup_observability_data(
        older_than_days=older_than_days,
        apply=apply,
        include_audit_events=include_audit_events,
    )
    click.echo(_summary(result, apply=apply))
    if result["errors"]:
        raise click.ClickException("; ".join(result["errors"]))
