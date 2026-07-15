import signal
import time

import click
from flask import current_app

from worker.reconciliation.service import reconcile_deployments
from worker.work_items import process_next_work_item


_keep_running = True


def _request_shutdown(_signum, _frame):
    global _keep_running
    _keep_running = False


def run_worker_loop(*, interval, sleep_fn=time.sleep, processor=process_next_work_item, emitter=click.echo):
    global _keep_running
    while _keep_running:
        deployment = processor()
        if deployment is None:
            sleep_fn(interval)
            continue

        emitter(f"Processed deployment {deployment.id} with final status '{deployment.status}'")


def run_reconciler_loop(*, interval, sleep_fn=time.sleep, reconciler=reconcile_deployments, emitter=click.echo):
    global _keep_running
    while _keep_running:
        changes = reconciler(emitter=emitter)
        emitter(f"Reconciler finished with {changes} action(s)")
        if _keep_running:
            sleep_fn(interval)


@click.command("run-worker-once")
def run_worker_once():
    deployment = process_next_work_item()
    if deployment is None:
        click.echo("No pending deployments found")
        return

    click.echo(f"Processed deployment {deployment.id} with final status '{deployment.status}'")


@click.command("run-worker")
@click.option("--poll-interval", type=float, default=None, help="Seconds to sleep between polling iterations.")
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
    click.echo(f"Worker polling every {interval} seconds")
    run_worker_loop(interval=interval)
    click.echo("Worker stopped")


def _run_reconciler_once():
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
@click.option("--poll-interval", type=float, default=None, help="Seconds to sleep between reconciliation runs.")
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
    click.echo(f"Reconciler polling every {interval} seconds")
    run_reconciler_loop(interval=interval)
    click.echo("Reconciler stopped")
