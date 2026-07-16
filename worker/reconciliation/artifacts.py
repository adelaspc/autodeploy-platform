from control_plane.extensions import db
from worker.execution.contracts import WorkerExecutionError
from worker.reconciliation.events import record_reconcile_event


def reconcile_failed_artifacts(deployment, *, executor_factory):
    if deployment.status != "failed":
        return False

    changed = False
    executor = executor_factory(deployment)

    if deployment.deploy_target == "local-docker":
        try:
            if executor.container_exists(deployment):
                stop_result = executor.stop(deployment)
                record_reconcile_event(
                    deployment,
                    "reconcile.container_removed",
                    "Removed orphan container from failed deployment",
                    metadata=stop_result.metadata | {"log_path": stop_result.log_path},
                )
                changed = True
        except WorkerExecutionError as exc:
            record_reconcile_event(
                deployment,
                "reconcile.cleanup_failed",
                f"Failed to remove orphan container: {exc.message}",
                level="error",
                metadata=exc.metadata | {"step": exc.step},
            )
        except Exception as exc:
            record_reconcile_event(
                deployment,
                "reconcile.cleanup_failed",
                f"Failed to remove orphan container: {exc}",
                level="error",
                metadata={"action": "container_cleanup"},
            )

    try:
        cleanup_metadata = executor.cleanup_workspace(deployment)
        if cleanup_metadata.get("workspace_removed") or cleanup_metadata.get("log_removed"):
            record_reconcile_event(
                deployment,
                "reconcile.workspace_removed",
                "Removed stale workspace artifacts from failed deployment",
                metadata=cleanup_metadata,
            )
            changed = True
    except Exception as exc:
        record_reconcile_event(
            deployment,
            "reconcile.cleanup_failed",
            f"Failed to remove workspace artifacts: {exc}",
            level="error",
            metadata={"action": "workspace_cleanup"},
        )

    db.session.commit()
    return changed
