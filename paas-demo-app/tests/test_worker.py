from worker.executor import WorkerExecutionError
from worker.service import process_next_pending_deployment

from tests.test_projects import create_project


class FailingExecutor:
    def clone_repository(self, deployment):
        return "Repository cloned"

    def build_image(self, deployment):
        raise WorkerExecutionError("image.build", "Docker build failed")

    def run_tests(self, deployment):
        return "Tests completed"

    def push_image(self, deployment):
        return "Image pushed"

    def deploy_application(self, deployment):
        return "https://example.local"


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

    processed = process_next_pending_deployment()

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert processed.build.status == "succeeded"
    assert processed.service_url == "https://worker-success.local"

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "repository.clone_started" in event_types
    assert "tests.started" in event_types
    assert "tests.succeeded" in event_types
    assert "deployment.running" in event_types


def test_process_next_pending_deployment_skips_testing_when_no_test_command(client):
    pending = create_pending_deployment(client, name="worker-no-tests", test_command=None)

    processed = process_next_pending_deployment()

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"

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

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    event_types = [event["event_type"] for event in deployment["events"]]
    assert "image.build.failed" in event_types
    assert "deployment.failed" in event_types
