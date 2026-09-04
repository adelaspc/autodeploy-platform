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


def cleanup_observability_data(*, older_than_days, apply=False, include_audit_events=False, now=None):
    cutoff = retention_cutoff(older_than_days=older_than_days, now=now)
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
