"""Provide worker and reconciler entrypoints for one-shot and polling modes."""

import signal
import time

import click
from flask import current_app
from sqlalchemy import text

from control_plane.extensions import db
from worker.reconciliation.service import reconcile_deployments
from worker.work_items import process_next_work_item


_keep_running = True


def _request_shutdown(_signum, _frame):
    global _keep_running
    _keep_running = False


def run_worker_loop(*, interval, sleep_fn=time.sleep, processor=process_next_work_item, emitter=None):
    global _keep_running
    while _keep_running:
        deployment = processor()
        if deployment is None:
            sleep_fn(interval)
            continue

        message = f"Processed deployment {deployment.id} with final status '{deployment.status}'"
        if emitter is not None:
            emitter(message)
        else:
            current_app.logger.info(
                "worker_deployment_processed",
                extra={
                    "event": "worker_deployment_processed",
                    "request_id": deployment.origin_request_id,
                    "project_id": deployment.project_id,
                    "deployment_id": deployment.id,
                    "build_id": deployment.build_id,
                    "final_status": deployment.status,
                },
            )


def run_reconciler_loop(*, interval, sleep_fn=time.sleep, reconciler=reconcile_deployments, emitter=None):
    global _keep_running
    while _keep_running:
        changes = reconciler(emitter=emitter)
        message = f"Reconciler finished with {changes} action(s)"
        if emitter is not None:
            emitter(message)
        else:
            current_app.logger.info(
                "reconciler_pass_completed",
                extra={
                    "event": "reconciler_pass_completed",
                    "reconciliation_actions": changes,
                },
            )
        if _keep_running:
            sleep_fn(interval)


@click.command("run-worker-once")
def run_worker_once():
    deployment = process_next_work_item()
    if deployment is None:
        click.echo("No pending deployments found")
        return

    click.echo(f"Processed deployment {deployment.id} with final status '{deployment.status}'")


@click.command("check-worker-readiness")
def check_worker_readiness():
    """Verify that the worker can reach its database and has a usable executor configuration."""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        current_app.logger.warning(
            "Worker readiness database check failed",
            extra={"event": "worker_readiness_database_failed", "error_type": type(exc).__name__},
        )
        raise click.ClickException("worker database is unreachable") from exc

    try:
        from control_plane.application.projects.read_models import platform_status_payload

        status = platform_status_payload()
    except Exception as exc:
        current_app.logger.warning(
            "Worker readiness executor check failed",
            extra={"event": "worker_readiness_executor_failed", "error_type": type(exc).__name__},
        )
        raise click.ClickException("worker executor configuration is not ready") from exc
    if not status.get("deployment_creation_ready"):
        raise click.ClickException("worker executor configuration is not ready")


@click.command("run-worker")
@click.option("--poll-interval", type=click.FloatRange(min=0, min_open=True), default=None, help="Seconds to sleep between polling iterations.")
def run_worker(poll_interval):
    global _keep_running
    _keep_running = True
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    configured_interval = current_app.config.get(
        "CONTROL_PLANE_WORKER_POLL_INTERVAL_SECONDS",
        5.0,
    )
    interval = configured_interval if poll_interval is None else poll_interval
    current_app.logger.info(
        "worker_started",
        extra={"event": "worker_started", "poll_interval_seconds": interval},
    )
    run_worker_loop(interval=interval)
    current_app.logger.info("worker_stopped", extra={"event": "worker_stopped"})


def _run_reconciler_once():
    if current_app.config.get("CONTROL_PLANE_COMPONENT") == "reconciler":
        changes = reconcile_deployments()
        current_app.logger.info(
            "reconciler_pass_completed",
            extra={
                "event": "reconciler_pass_completed",
                "reconciliation_actions": changes,
            },
        )
        return

    changes = reconcile_deployments(emitter=click.echo)
    click.echo(f"Reconciler finished with {changes} action(s)")


@click.command("run-reconciler-once")
def run_reconciler_once():
    _run_reconciler_once()


@click.command("run-reconciler", hidden=True)
def run_reconciler():
    """Backward-compatible alias for run-reconciler-once."""
    _run_reconciler_once()


@click.command("run-reconciler-loop")
@click.option("--poll-interval", type=click.FloatRange(min=0, min_open=True), default=None, help="Seconds to sleep between reconciliation runs.")
def run_reconciler_loop_command(poll_interval):
    global _keep_running
    _keep_running = True
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    configured_interval = current_app.config.get(
        "CONTROL_PLANE_RECONCILER_POLL_INTERVAL_SECONDS",
        60.0,
    )
    interval = configured_interval if poll_interval is None else poll_interval
    current_app.logger.info(
        "reconciler_started",
        extra={"event": "reconciler_started", "poll_interval_seconds": interval},
    )
    run_reconciler_loop(interval=interval)
    current_app.logger.info("reconciler_stopped", extra={"event": "reconciler_stopped"})
