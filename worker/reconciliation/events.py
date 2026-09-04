"""Record reconciliation decisions in the deployment timeline."""

from worker.processing.events import record_event


def record_reconcile_event(deployment, event_type, message, *, level="info", metadata=None, step="reconcile"):
    record_event(
        deployment,
        event_type,
        deployment.status,
        message,
        step=step,
        level=level,
        metadata=metadata,
    )
