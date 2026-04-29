import click

from worker.service import process_next_pending_deployment


@click.command("run-worker-once")
def run_worker_once():
    deployment = process_next_pending_deployment()
    if deployment is None:
        click.echo("No pending deployments found")
        return

    click.echo(f"Processed deployment {deployment.id} with final status '{deployment.status}'")
