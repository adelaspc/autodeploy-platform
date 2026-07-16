import shutil
import subprocess
import textwrap

import pytest

from worker.executors.local_docker import LocalDockerExecutor
from worker.processing.command_processor import process_next_pending_command
from worker.processing.pipeline import process_next_pending_deployment

pytestmark = pytest.mark.docker


def _docker_available():
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    completed = subprocess.run([docker_bin, "version"], capture_output=True, text=True, timeout=10, check=False)
    return completed.returncode == 0


def _create_repo(repo_dir, *, health_response="ok", health_status=200):
    repo_dir.mkdir()
    (repo_dir / "Dockerfile").write_text(
        textwrap.dedent(
            """
            FROM python:3.12-slim
            WORKDIR /app
            COPY server.py /app/server.py
            EXPOSE 5000
            CMD ["python", "/app/server.py"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (repo_dir / "server.py").write_text(
        textwrap.dedent(
            f"""
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/health":
                        self.send_response({health_status})
                        self.end_headers()
                        self.wfile.write({health_response.encode()!r})
                        return
                    self.send_response(404)
                    self.end_headers()

                def log_message(self, format, *args):
                    return

            HTTPServer(("0.0.0.0", 5000), Handler).serve_forever()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True, text=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, text=True, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AutoDeploy Tests",
            "-c",
            "user.email=autodeploy-tests@example.invalid",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return commit.stdout.strip()


def test_create_repo_does_not_require_global_git_identity(tmp_path):
    repo_dir = tmp_path / "identity-independent-repo"

    commit_sha = _create_repo(repo_dir)

    commit = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    assert commit.stdout.strip() == "AutoDeploy Tests <autodeploy-tests@example.invalid>"
    assert commit_sha == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
def test_local_docker_executor_real_container_flow(client, tmp_path):
    repo_dir = tmp_path / "repo"
    commit_sha = _create_repo(repo_dir)

    project_response = client.post(
        "/api/projects",
        json={
            "name": "real-local-docker-app",
            "repo_url": str(repo_dir),
            "branch": "main",
            "dockerfile_path": "Dockerfile",
            "build_context": ".",
            "port": 5000,
            "healthcheck_path": "/health",
            "env_vars": [],
            "trigger": "manual",
            "runtime": "dockerfile",
        },
    )
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": commit_sha,
            "image_name": "real-local-docker-app",
            "image_tag": "realdocker001",
            "status": "pending",
            "build_status": "pending",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    executor = LocalDockerExecutor(
        workspace_root=tmp_path / "workspaces",
        command_timeout=120,
        retry_count=0,
        healthcheck_timeout=30,
        healthcheck_interval=1,
    )

    processed = None
    container_name = None
    try:
        processed = process_next_pending_deployment(executor=executor)
        assert processed is not None
        assert processed.id == deployment_id
        assert processed.status == "running"

        deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
        apply_event = next(event for event in deployment["events"] if event["event_type"] == "deployment.apply_succeeded")
        container_name = apply_event["metadata_json"]["container_name"]
        assert apply_event["metadata_json"]["healthcheck_status_code"] == 200
        assert apply_event["metadata_json"]["runtime_log_path"].endswith("/runtime.log")
        assert deployment["service_url"].startswith("http://127.0.0.1:")

        stop_response = client.post(
            f"/api/projects/{project_id}/deployments/{deployment_id}/stop",
        )
        assert stop_response.status_code == 202
        stopped_deployment = process_next_pending_command(executor=executor)
        assert stopped_deployment is not None
        assert stopped_deployment.id == deployment_id
        assert stopped_deployment.status == "stopped"

        stopped = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
        assert stopped["status"] == "stopped"
        assert stopped["service_url"] is None
        stopped_event = next(event for event in stopped["events"] if event["event_type"] == "deployment.stopped")
        assert stopped_event["metadata_json"]["runtime_log_path"].endswith("/runtime.log")
        container_name = None
    finally:
        if container_name:
            subprocess.run(
                ["docker", "rm", "--force", container_name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )


@pytest.mark.skipif(not _docker_available(), reason="Docker not available")
def test_local_docker_executor_healthcheck_failure_persists_runtime_log(client, tmp_path):
    repo_dir = tmp_path / "repo-fail"
    commit_sha = _create_repo(repo_dir, health_response="not ready", health_status=503)

    project_response = client.post(
        "/api/projects",
        json={
            "name": "real-local-docker-fail-app",
            "repo_url": str(repo_dir),
            "branch": "main",
            "dockerfile_path": "Dockerfile",
            "build_context": ".",
            "port": 5000,
            "healthcheck_path": "/health",
            "env_vars": [],
            "trigger": "manual",
            "runtime": "dockerfile",
        },
    )
    project_id = project_response.get_json()["id"]
    deployment_response = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": commit_sha,
            "image_name": "real-local-docker-fail-app",
            "image_tag": "realdocker002",
            "status": "pending",
            "build_status": "pending",
        },
    )
    deployment_id = deployment_response.get_json()["id"]

    executor = LocalDockerExecutor(
        workspace_root=tmp_path / "workspaces",
        command_timeout=120,
        retry_count=0,
        healthcheck_timeout=5,
        healthcheck_interval=1,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == deployment_id
    assert processed.status == "failed"
    assert processed.last_error == "Healthcheck did not succeed within 5 seconds"

    deployment = client.get(f"/api/projects/{project_id}/deployments/{deployment_id}").get_json()
    failed_events = [event for event in deployment["events"] if event["event_type"] == "deployment.failed"]
    assert len(failed_events) == 1
    failure_metadata = failed_events[0]["metadata_json"]
    assert failure_metadata["runtime_log_path"].endswith("/runtime.log")
    assert "not ready" in (failure_metadata["runtime_log_summary"] or "")
    assert failure_metadata["container_name"].startswith("paas-real-local-docker-fail-app-")
