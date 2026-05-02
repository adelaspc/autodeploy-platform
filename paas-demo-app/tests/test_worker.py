from datetime import timedelta
from types import SimpleNamespace

from backend.extensions import db
from backend.models import PlatformDeployment
from worker.cli import run_worker_loop
from pathlib import Path
import subprocess

from worker.executor import ExecutionResult, LocalDockerExecutor, WorkerExecutionError
import worker.service as worker_service
from worker.service import claim_next_pending_deployment, now_utc, process_next_pending_deployment, refresh_claim

from tests.test_projects import create_project


class OrderedExecutor:
    def __init__(self):
        self.calls = []

    def clone_repo(self, deployment):
        self.calls.append("clone_repo")
        return ExecutionResult(
            "Repository cloned",
            metadata={"executor": "ordered"},
            workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}",
        )

    def build_image(self, deployment):
        self.calls.append("build_image")
        return ExecutionResult(
            "Docker image built",
            metadata={"executor": "ordered"},
            image_tag=f"{deployment.project.name}:abc123def456",
            image_ref=f"{deployment.project.name}:abc123def456",
            log_path="/tmp/test-workspaces/build.log",
        )

    def run_tests(self, deployment):
        self.calls.append("run_tests")
        return ExecutionResult(
            "Tests passed",
            metadata={"executor": "ordered"},
            log_path="/tmp/test-workspaces/tests.log",
        )

    def tag_image(self, deployment):
        self.calls.append("tag_image")
        return ExecutionResult(
            "Image tag skipped",
            metadata={"executor": "ordered", "skipped": True},
            image_tag=deployment.build.image_tag,
            image_ref=deployment.build.image_ref,
            log_path="/tmp/test-workspaces/tag.log",
        )

    def push_image(self, deployment):
        self.calls.append("push_image")
        return ExecutionResult(
            "Push skipped",
            metadata={"executor": "ordered", "skipped": True},
            log_path="/tmp/test-workspaces/push.log",
        )

    def deploy(self, deployment):
        self.calls.append("deploy")
        return ExecutionResult(
            "Deployment simulated",
            metadata={"executor": "ordered"},
            log_path="/tmp/test-workspaces/deploy.log",
            service_url=f"https://{deployment.project.name}.local",
            deploy_target="ordered",
        )


class FailingExecutor:
    def clone_repo(self, deployment):
        return ExecutionResult(
            "Repository cloned",
            metadata={"executor": "failing"},
            workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}",
            log_path="/tmp/test-workspaces/clone.log",
        )

    def build_image(self, deployment):
        raise WorkerExecutionError(
            "image.build",
            "Docker build failed",
            log_path="/tmp/test-workspaces/build.log",
        )

    def run_tests(self, deployment):
        return ExecutionResult("Tests completed")

    def tag_image(self, deployment):
        return ExecutionResult("Image tagged")

    def push_image(self, deployment):
        return ExecutionResult("Image pushed")

    def deploy(self, deployment):
        return ExecutionResult("Deployed", service_url="https://example.local", deploy_target="failing")


class PushFailingExecutor:
    def clone_repo(self, deployment):
        return ExecutionResult("Repository cloned", workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}")

    def build_image(self, deployment):
        return ExecutionResult(
            "Docker image built",
            image_tag=f"{deployment.project.name}:abc123def456",
            image_ref=f"registry.example.com/paas/{deployment.project.name}:abc123def456",
        )

    def run_tests(self, deployment):
        return ExecutionResult("Tests passed")

    def tag_image(self, deployment):
        return ExecutionResult("Image tagged", image_tag=deployment.build.image_tag, image_ref=deployment.build.image_ref)

    def push_image(self, deployment):
        raise WorkerExecutionError(
            "image.push",
            "Registry push failed",
            metadata={"summary": "Registry push failed", "output_tail": ["denied: push failed"]},
            log_path="/tmp/test-workspaces/push.log",
        )

    def deploy(self, deployment):
        raise AssertionError("deploy should not run after push failure")


class ClaimObservingExecutor:
    def __init__(self):
        self.claim_timestamps = []

    def clone_repo(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult("Repository cloned", workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}")

    def build_image(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult(
            "Docker image built",
            image_tag=f"{deployment.project.name}:abc123def456",
            image_ref=f"{deployment.project.name}:abc123def456",
        )

    def run_tests(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult("Tests passed")

    def tag_image(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult("Image tag skipped", image_tag=deployment.build.image_tag, image_ref=deployment.build.image_ref)

    def push_image(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult("Push skipped", metadata={"skipped": True})

    def deploy(self, deployment):
        self.claim_timestamps.append(deployment.claimed_at)
        return ExecutionResult("Deployment simulated", service_url=f"https://{deployment.project.name}.local", deploy_target="observing")


class ClaimLosingExecutor:
    def clone_repo(self, deployment):
        return ExecutionResult("Repository cloned", workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}")

    def build_image(self, deployment):
        db.session.execute(
            worker_service.update(PlatformDeployment)
            .where(PlatformDeployment.id == deployment.id)
            .values(claimed_by="other-worker", claimed_at=worker_service.now_utc())
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
        return ExecutionResult("Docker image built", image_tag="claim-loss:abc123def456", image_ref="claim-loss:abc123def456")

    def run_tests(self, deployment):
        return ExecutionResult("Tests passed")

    def tag_image(self, deployment):
        return ExecutionResult("Image tagged")

    def push_image(self, deployment):
        return ExecutionResult("Push skipped")

    def deploy(self, deployment):
        return ExecutionResult("Deployed", service_url="https://example.local", deploy_target="claim-loss")


def create_pending_deployment(client, *, name="worker-app", test_command="pytest -q"):
    project_response = create_project(client, name=name)
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "registry": "ghcr.io/example",
            "image_name": name,
            "image_tag": "abc123def456",
            "status": "pending",
            "build_status": "pending",
            "test_command": test_command,
        },
    )
    return deployment_response.get_json()


def test_process_next_pending_deployment_runs_to_completion(client):
    pending = create_pending_deployment(client, name="worker-success")
    executor = OrderedExecutor()

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert processed.build.status == "succeeded"
    assert processed.deploy_target == "ordered"
    assert processed.service_url == "https://worker-success.local"
    assert processed.started_at is not None
    assert processed.finished_at is not None
    assert processed.claimed_at is None
    assert processed.claimed_by is None
    assert processed.build.started_at is not None
    assert processed.build.finished_at is not None
    assert executor.calls == ["clone_repo", "build_image", "run_tests", "tag_image", "push_image", "deploy"]

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "repository.clone_started" in event_types
    assert "repository.clone_succeeded" in event_types
    assert "tests.started" in event_types
    assert "tests.succeeded" in event_types
    assert "image.tag_started" in event_types
    assert "image.tag_succeeded" in event_types
    assert "deployment.running" in event_types
    assert "claim_acquired" in event_types
    assert "claim_cleared" in event_types
    assert all(event["level"] in {"info", "error"} for event in deployment["events"])
    assert any(event["metadata_json"] for event in deployment["events"])


def test_process_next_pending_deployment_skips_testing_when_no_test_command(client):
    pending = create_pending_deployment(client, name="worker-no-tests", test_command=None)
    executor = OrderedExecutor()

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert "run_tests" not in executor.calls

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "tests.started" not in event_types
    assert "tests.succeeded" not in event_types


def test_process_next_pending_deployment_marks_failure_and_records_events(client):
    pending = create_pending_deployment(client, name="worker-failure")

    processed = process_next_pending_deployment(executor=FailingExecutor())

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "failed"
    assert processed.build.status == "failed"
    assert processed.last_error == "Docker build failed"
    assert processed.build.last_error == "Docker build failed"
    assert processed.build.log_path == "/tmp/test-workspaces/build.log"
    assert processed.claimed_at is None
    assert processed.claimed_by is None

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "image.build.failed" in event_types
    assert "deployment.failed" in event_types
    assert "claim_cleared" in event_types
    failed_event = next(event for event in deployment["events"] if event["event_type"] == "deployment.failed")
    assert failed_event["level"] == "error"
    assert failed_event["metadata_json"]["log_path"] == "/tmp/test-workspaces/build.log"
    assert failed_event["metadata_json"]["summary"] == "Docker build failed"


def test_process_next_pending_deployment_stops_after_push_failure(client):
    pending = create_pending_deployment(client, name="worker-push-failure")

    processed = process_next_pending_deployment(executor=PushFailingExecutor())

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "failed"
    assert processed.build.status == "failed"
    assert processed.last_error == "Registry push failed"
    assert processed.build.log_path == "/tmp/test-workspaces/push.log"

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "image.tag_succeeded" in event_types
    assert "image.push.failed" in event_types
    assert "deployment.apply_started" not in event_types


def test_run_worker_loop_polls_until_stopped():
    calls = {"count": 0}
    messages = []

    def processor():
        calls["count"] += 1
        if calls["count"] == 1:
            return SimpleNamespace(id=99, status="running")
        return None

    def sleep_fn(_interval):
        import worker.cli as worker_cli

        worker_cli._keep_running = False

    import worker.cli as worker_cli

    worker_cli._keep_running = True
    run_worker_loop(interval=0.01, processor=processor, sleep_fn=sleep_fn, emitter=messages.append)

    assert calls["count"] == 2
    assert messages == ["Processed deployment 99 with final status 'running'"]


def test_claim_next_pending_deployment_skips_fresh_claims(client, app):
    first = create_pending_deployment(client, name="claimed-first")
    second = create_pending_deployment(client, name="claimed-second")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKER_ID"] = "worker-a"
        claimed = claim_next_pending_deployment(claim_ttl_seconds=300)
        assert claimed is not None
        assert claimed.id == first["id"]

        db.session.expire_all()
        first_row = db.session.get(PlatformDeployment, first["id"])
        assert first_row.claimed_by == "worker-a"
        assert first_row.claimed_at is not None

        app.config["CONTROL_PLANE_WORKER_ID"] = "worker-b"
        next_claimed = claim_next_pending_deployment(claim_ttl_seconds=300)
        assert next_claimed is not None
        assert next_claimed.id == second["id"]


def test_claim_next_pending_deployment_reclaims_stale_claims(client, app):
    pending = create_pending_deployment(client, name="stale-claim")

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, pending["id"])
        deployment.claimed_at = now_utc() - timedelta(seconds=600)
        deployment.claimed_by = "dead-worker"
        db.session.commit()

        app.config["CONTROL_PLANE_WORKER_ID"] = "worker-c"
        claimed = claim_next_pending_deployment(claim_ttl_seconds=300)

        assert claimed is not None
        assert claimed.id == pending["id"]
        assert claimed.claimed_by == "worker-c"


def test_worker_refreshes_claim_during_processing(client):
    create_pending_deployment(client, name="claim-refresh")
    executor = ClaimObservingExecutor()

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.status == "running"
    assert len(executor.claim_timestamps) == 6
    assert all(timestamp is not None for timestamp in executor.claim_timestamps)
    assert executor.claim_timestamps == sorted(executor.claim_timestamps)


def test_second_worker_cannot_reclaim_actively_refreshed_claim(client, app):
    pending = create_pending_deployment(client, name="active-claim")

    with app.app_context():
        app.config["CONTROL_PLANE_WORKER_ID"] = "worker-a"
        claimed = claim_next_pending_deployment(claim_ttl_seconds=1)
        assert claimed is not None
        refresh_claim(claimed, expected_worker_id="worker-a")
        db.session.commit()

        app.config["CONTROL_PLANE_WORKER_ID"] = "worker-b"
        reclaimed = claim_next_pending_deployment(claim_ttl_seconds=1)
        assert reclaimed is None

        claimed_row = db.session.get(PlatformDeployment, pending["id"])
        assert claimed_row.claimed_by == "worker-a"


def test_long_running_worker_step_does_not_get_reclaimed(client, app):
    pending = create_pending_deployment(client, name="long-running")

    class LongStepExecutor:
        def clone_repo(self, deployment):
            with app.app_context():
                app.config["CONTROL_PLANE_WORKER_ID"] = "worker-b"
                assert claim_next_pending_deployment(claim_ttl_seconds=0) is None
                app.config["CONTROL_PLANE_WORKER_ID"] = "worker"
            return ExecutionResult(
                "Repository cloned",
                workspace_path=f"/tmp/test-workspaces/deployment-{deployment.id}",
            )

        def build_image(self, deployment):
            return ExecutionResult("Docker image built", image_tag="long-running:abc123def456", image_ref="long-running:abc123def456")

        def run_tests(self, deployment):
            return ExecutionResult("Tests passed")

        def tag_image(self, deployment):
            return ExecutionResult("Image tagged")

        def push_image(self, deployment):
            return ExecutionResult("Push skipped")

        def deploy(self, deployment):
            return ExecutionResult("Deployment simulated", service_url="https://long-running.local", deploy_target="long")

    processed = process_next_pending_deployment(executor=LongStepExecutor())

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"


def test_worker_does_not_mark_running_when_claim_is_lost(client):
    pending = create_pending_deployment(client, name="claim-loss")

    processed = process_next_pending_deployment(executor=ClaimLosingExecutor())

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "building"
    assert processed.claimed_by == "other-worker"

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "claim_lost" in event_types
    assert "deployment.running" not in event_types


def test_local_docker_executor_heartbeats_during_long_command(client, app, tmp_path):
    pending = create_pending_deployment(client, name="heartbeat-local", test_command=None)
    heartbeat_calls = {"count": 0}

    def heartbeat_runner(args, capture_output, text, timeout, check, input=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            if heartbeat_cb is not None:
                heartbeat_cb()
                heartbeat_calls["count"] += 1
                with app.app_context():
                    app.config["CONTROL_PLANE_WORKER_ID"] = "worker-b"
                    assert claim_next_pending_deployment(claim_ttl_seconds=0) is None
                    app.config["CONTROL_PLANE_WORKER_ID"] = "worker"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ready\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{args[0]} ok\n", stderr="")

    def fake_health_probe(_url):
        return {"status_code": 200, "summary": "ok"}

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=heartbeat_runner,
        port_allocator=lambda: 18080,
        health_probe=fake_health_probe,
        heartbeat_interval=0,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert heartbeat_calls["count"] == 1
