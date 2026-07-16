from pathlib import Path
import subprocess
import base64
import sys
from io import BytesIO
from urllib.error import HTTPError
import pytest

from control_plane.extensions import db
from control_plane.models import PlatformDeployment
from worker.execution.contracts import WorkerExecutionError
from worker.executors.local_docker import LocalDockerExecutor
from worker.services.healthcheck import _NoRedirectHandler
from worker.processing.pipeline import process_next_pending_deployment

from tests.api.test_projects import create_project
from tests.execution.test_worker import create_pending_deployment


def test_local_docker_executor_processes_deployment_with_stubbed_commands(client, tmp_path):
    pending = create_pending_deployment(client, name="local-executor-app", test_command="pytest -q")
    commands = []

    def fake_runner(args, capture_output, text, timeout, check):
        commands.append(args)
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="app booted\nready\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{args[0]} ok\n", stderr="")

    def fake_health_probe(_url):
        return {"status_code": 200, "summary": "ok"}

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        port_allocator=lambda: 18080,
        health_probe=fake_health_probe,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed is not None
    assert processed.id == pending["id"]
    assert processed.status == "running"
    assert processed.deploy_target == "local-docker"
    assert processed.build.workspace_path.startswith(str(tmp_path))
    assert processed.build.image_tag == "local-executor-app:abc123def456"
    assert processed.build.image_ref == "local-executor-app:abc123def456"

    assert [command[:2] for command in commands[:3]] == [
        ["git", "clone"],
        ["git", "-C"],
        ["docker", "build"],
    ]
    assert commands[3][:3] == ["docker", "run", "--rm"]
    assert commands[4][:3] == ["docker", "rm", "--force"]
    assert commands[5][:3] == ["docker", "run", "--detach"]
    assert commands[6][:2] == ["docker", "logs"]

    deployment_response = client.get(f"/api/projects/{processed.project_id}/deployments/{processed.id}")
    deployment = deployment_response.get_json()
    apply_events = [event for event in deployment["events"] if event["event_type"] == "deployment.apply_succeeded"]
    assert len(apply_events) == 1
    assert apply_events[0]["metadata_json"]["deploy_target"] == "local-docker"
    assert apply_events[0]["metadata_json"]["summary"] == "Container started and passed healthcheck."
    assert apply_events[0]["metadata_json"]["container_id"] == "container123"
    assert apply_events[0]["metadata_json"]["host_port"] == 18080
    assert apply_events[0]["metadata_json"]["healthcheck_status_code"] == 200
    assert apply_events[0]["metadata_json"]["runtime_log_summary"] == "app booted | ready"
    assert apply_events[0]["metadata_json"]["runtime_log_path"].endswith("/runtime.log")
    assert deployment["service_url"] == "http://127.0.0.1:18080"


def test_worker_uses_deployment_snapshot_after_project_is_edited(client, app, tmp_path):
    project_id = create_project(
        client,
        name="snapshot-app",
        repo_url="https://github.com/example/original-repo",
        branch="main",
        dockerfile_path="Dockerfile",
        build_context=".",
        port=5000,
        healthcheck_path="/health",
        env_vars=[{"name": "APP_MODE", "value": "original"}],
    ).get_json()["id"]
    deployment_id = client.post(
        f"/api/projects/{project_id}/deployments",
        json={
            "commit_sha": "abc123def456",
            "image_name": "snapshot-app",
            "image_tag": "abc123def456",
            "status": "pending",
            "build_status": "pending",
            "test_command": None,
        },
    ).get_json()["id"]

    with app.app_context():
        deployment = db.session.get(PlatformDeployment, deployment_id)
        deployment.project.name = "edited-app"
        deployment.project.repo_url = "https://github.com/example/edited-repo"
        deployment.project.branch = "edited"
        deployment.project.dockerfile_path = "docker/Edited.Dockerfile"
        deployment.project.build_context = "edited-context"
        deployment.project.port = 9000
        deployment.project.healthcheck_path = "/edited-health"
        deployment.project.env_vars = [{"name": "APP_MODE", "value": "edited"}]
        db.session.commit()

    commands = []
    health_urls = []

    def fake_runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ready\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    def health_probe(url):
        health_urls.append(url)
        return {"status_code": 200, "summary": "ok"}

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
        port_allocator=lambda: 18080,
        health_probe=health_probe,
    )

    processed = process_next_pending_deployment(executor=executor)

    clone_command = next(command for command in commands if command[:2] == ["git", "clone"])
    build_command = next(command for command in commands if command[:2] == ["docker", "build"])
    run_command = next(command for command in commands if command[:3] == ["docker", "run", "--detach"])
    assert processed.id == deployment_id
    assert processed.status == "running"
    assert clone_command[3] == "main"
    assert clone_command[5] == "https://github.com/example/original-repo.git"
    assert build_command[5].endswith("/repo/Dockerfile")
    assert build_command[6].endswith("/repo")
    assert f"paas-snapshot-app-{deployment_id}" in run_command
    assert "127.0.0.1:18080:5000" in run_command
    assert "APP_MODE=original" in run_command
    assert health_urls == ["http://127.0.0.1:18080/health"]


def test_local_docker_healthcheck_rejects_redirect_status(tmp_path):
    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        health_probe=lambda _url: {"status_code": 302, "summary": "redirect"},
        healthcheck_timeout=0.001,
        healthcheck_interval=0,
        sleep_fn=lambda _seconds: None,
    )
    log_path = tmp_path / "healthcheck.log"

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor._wait_for_healthcheck("http://demo.example/health", log_path)

    assert exc_info.value.step == "deploy.healthcheck"
    assert exc_info.value.metadata["healthcheck_last_error"] == "HTTP 302"
    assert "failed: 302 redirect" in log_path.read_text(encoding="utf-8")


def test_default_health_probe_does_not_follow_redirects(tmp_path):
    def redirecting_opener(url, *, timeout):
        raise HTTPError(url, 302, "Found", {"Location": "/healthy"}, BytesIO(b""))

    executor = LocalDockerExecutor(workspace_root=tmp_path, command_timeout=30)
    result = executor._default_health_probe(
        "http://demo.example/redirect",
        opener=redirecting_opener,
    )

    assert _NoRedirectHandler().redirect_request(None, None, 302, "Found", {}, "/healthy") is None
    assert result["status_code"] == 302
    assert result["summary"] == "HTTP 302"


def test_cleanup_workspace_rejects_paths_outside_workspace_root(tmp_path):
    executor = LocalDockerExecutor(workspace_root=tmp_path / "workspaces", command_timeout=30)
    outside = tmp_path / "outside"
    outside.mkdir()
    build = type("BuildStub", (), {"workspace_path": str(outside), "log_path": None})()
    deployment = type("DeploymentStub", (), {"build": build})()

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor.cleanup_workspace(deployment)

    assert exc_info.value.step == "deployment.cleanup"
    assert outside.exists()


def test_local_docker_executor_rejects_invalid_persisted_test_command(client, tmp_path):
    pending = create_pending_deployment(client, name="invalid-persisted-command-app", test_command="pytest -q")
    deployment = db.session.get(PlatformDeployment, pending["id"])
    deployment.build.test_command = "pytest -q && curl https://example.test"
    db.session.commit()
    commands = []

    def fake_runner(args, capture_output, text, timeout, check):
        commands.append(args)
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{args[0]} ok\n", stderr="")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=fake_runner,
    )

    processed = process_next_pending_deployment(executor=executor)

    assert processed.id == pending["id"]
    assert processed.status == "failed"
    assert processed.build.status == "failed"
    assert "Shell control" in processed.build.last_error
    assert not any(command[:3] == ["docker", "run", "--rm"] for command in commands)


def test_local_docker_executor_pushes_registry_image_when_enabled(tmp_path):
    commands = []

    def registry_runner(args, capture_output, text, timeout, check, input=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append({"args": args, "input": input})
        if args[:2] == ["git", "clone"]:
            repo_dir = Path(args[-1])
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")
        if args[:3] == ["docker", "login", "registry.example.com"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="login ok\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ready\n", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"{args[0]} ok\n", stderr="")

    def fake_health_probe(_url):
        return {"status_code": 200, "summary": "ok"}

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=registry_runner,
        port_allocator=lambda: 18080,
        health_probe=fake_health_probe,
        registry_enabled=True,
        registry_url="registry.example.com",
        registry_namespace="paas",
        registry_username="ci-user",
        registry_password="super-secret",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type(
                "BuildStub",
                (),
                {
                    "commit_sha": "abc123def456",
                    "image_name": "demo-app",
                    "image_tag": "abc123def456",
                    "image_ref": None,
                    "test_command": None,
                },
            )(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo-app",
                    "branch": "main",
                    "repo_url": "/tmp/repo",
                    "dockerfile_path": "Dockerfile",
                    "build_context": ".",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [],
                },
            )(),
        },
    )()

    clone_result = executor.clone_repo(deployment)
    assert clone_result.workspace_path.startswith(str(tmp_path))
    build_result = executor.build_image(deployment)
    deployment.build.image_tag = build_result.image_tag
    deployment.build.image_ref = build_result.image_ref
    tag_result = executor.tag_image(deployment)
    push_result = executor.push_image(deployment)
    verify_result = executor.verify_image(deployment)

    assert build_result.image_tag == "demo-app:abc123def456"
    assert build_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert tag_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert push_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert verify_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    build_call = next(item for item in commands if item["args"][:2] == ["docker", "build"])
    assert build_call["args"][3] == "demo-app:abc123def456"
    assert any(item["args"][:2] == ["docker", "tag"] for item in commands)
    assert any(item["args"][:2] == ["docker", "push"] for item in commands)
    assert any(item["args"][:4] == ["docker", "buildx", "imagetools", "inspect"] for item in commands)
    login_call = next(item for item in commands if item["args"][:2] == ["docker", "login"])
    assert login_call["input"] == "super-secret"
    assert "super-secret" not in " ".join(" ".join(item["args"]) for item in commands)


def test_local_docker_executor_fails_when_registry_image_verification_fails(tmp_path):
    def verify_runner(args, capture_output, text, timeout, check, input=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="manifest unknown\n")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=verify_runner,
        registry_enabled=True,
        registry_url="registry.example.com",
        registry_namespace="paas",
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type(
                "BuildStub",
                (),
                {
                    "image_tag": "demo-app:abc123def456",
                    "image_ref": "registry.example.com/paas/demo-app:abc123def456",
                },
            )(),
            "project": type("ProjectStub", (), {"env_vars": []})(),
        },
    )()

    try:
        executor.verify_image(deployment)
        raise AssertionError("verify_image should fail when registry image inspection fails")
    except WorkerExecutionError as exc:
        assert exc.step == "image.verify"
        assert "manifest unknown" in exc.metadata["summary"]


def test_local_docker_executor_skips_push_when_registry_disabled(tmp_path):
    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args=kwargs.get("args", []), returncode=0, stdout="", stderr=""),
        registry_enabled=False,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"image_tag": "demo-app:abc123def456", "image_ref": "demo-app:abc123def456"})(),
            "project": type("ProjectStub", (), {"name": "demo-app"})(),
        },
    )()

    tag_result = executor.tag_image(deployment)
    push_result = executor.push_image(deployment)
    verify_result = executor.verify_image(deployment)

    assert tag_result.metadata["skipped"] is True
    assert push_result.metadata["skipped"] is True
    assert verify_result.metadata["skipped"] is True


def test_local_docker_executor_retries_retryable_steps(tmp_path):
    attempts = {"count": 0}

    def flaky_runner(args, capture_output, text, timeout, check):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="network hiccup\nretrying clone\n")
        repo_dir = Path(args[-1])
        repo_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone complete\n", stderr="")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=flaky_runner,
        retry_count=1,
        sleep_fn=lambda _seconds: None,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"commit_sha": "abc123def456"})(),
            "project": type("ProjectStub", (), {"branch": "main", "repo_url": "/tmp/repo"})(),
        },
    )()

    result = executor.clone_repo(deployment)

    assert attempts["count"] == 3
    assert result.metadata["attempt"] == 2
    assert result.metadata["total_attempts"] == 2
    log_contents = (tmp_path / "project-2" / "deployment-1" / "logs" / "clone.log").read_text(encoding="utf-8")
    assert "==== retry ====" in log_contents


def test_local_docker_executor_clones_private_github_repo_with_token_env(tmp_path, monkeypatch):
    commands = []

    def runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append({"args": args, "env": env})
        repo_dir = Path(args[-1])
        repo_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="clone ok\n", stderr="")

    monkeypatch.setenv("CONTROL_PLANE_GIT_TOKEN_DEMO_APP", "unit-test-github-token")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=runner,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"commit_sha": "abc123def456"})(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "branch": "main",
                    "repo_url": "https://github.com/example/private-repo",
                    "git_auth_type": "token",
                    "git_secret_ref": "DEMO_APP",
                },
            )(),
        },
    )()

    result = executor.clone_repo(deployment)

    assert result.workspace_path.startswith(str(tmp_path))
    clone_call = commands[0]
    assert clone_call["args"][:2] == ["git", "clone"]
    assert clone_call["args"][5] == "https://github.com/example/private-repo"
    assert clone_call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_call["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert clone_call["env"]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    encoded = clone_call["env"]["GIT_CONFIG_VALUE_0"].split("basic ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-8") == "x-access-token:unit-test-github-token"


def test_local_docker_executor_clones_recorded_branch_and_checks_out_recorded_commit(tmp_path):
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=source_repo, check=True, capture_output=True)

    def commit_file(contents, message):
        (source_repo / "version.txt").write_text(contents, encoding="utf-8")
        subprocess.run(["git", "add", "version.txt"], cwd=source_repo, check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=AutoDeploy Tests",
                "-c",
                "user.email=autodeploy-tests@example.invalid",
                "commit",
                "-m",
                message,
            ],
            cwd=source_repo,
            check=True,
            capture_output=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    commit_file("main\n", "main")
    subprocess.run(["git", "switch", "-c", "feature"], cwd=source_repo, check=True, capture_output=True)
    recorded_commit = commit_file("recorded\n", "recorded")
    commit_file("newer\n", "newer")

    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"commit_sha": recorded_commit})(),
            "project": type(
                "ProjectStub",
                (),
                {"branch": "main", "repo_url": str(source_repo), "git_auth_type": "none"},
            )(),
            "events": [
                type(
                    "EventStub",
                    (),
                    {"event_type": "deployment.created", "metadata_json": {"branch": "feature"}},
                )()
            ],
        },
    )()
    workspace_root = tmp_path / "workspaces"
    executor = LocalDockerExecutor(workspace_root=workspace_root, command_timeout=30)

    result = executor.clone_repo(deployment)

    repo_dir = workspace_root / "project-2" / "deployment-1" / "repo"
    checked_out_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert checked_out_commit == recorded_commit
    assert (repo_dir / "version.txt").read_text(encoding="utf-8") == "recorded\n"
    assert result.metadata["branch"] == "feature"
    assert result.metadata["commit_sha"] == recorded_commit


def test_command_runner_handles_output_larger_than_pipe_buffers(tmp_path):
    executor = LocalDockerExecutor(workspace_root=tmp_path, command_timeout=10)

    completed = executor._execute_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 2_000_000); sys.stderr.write('y' * 2_000_000)",
        ]
    )

    assert completed.returncode == 0
    assert len(completed.stdout) == 2_000_000
    assert len(completed.stderr) == 2_000_000


def test_local_docker_executor_clone_fails_when_git_token_env_is_missing(tmp_path):
    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args=kwargs.get("args", []), returncode=0, stdout="", stderr=""),
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "project": type(
                "ProjectStub",
                (),
                {
                    "branch": "main",
                    "repo_url": "https://github.com/example/private-repo",
                    "git_auth_type": "token",
                    "git_secret_ref": "MISSING_APP",
                },
            )(),
        },
    )()

    with pytest.raises(Exception) as exc_info:
        executor.clone_repo(deployment)

    assert "CONTROL_PLANE_GIT_TOKEN_MISSING_APP" in str(exc_info.value)


def test_local_docker_executor_reports_workspace_prepare_failure(tmp_path):
    workspace_file = tmp_path / "not-a-directory"
    workspace_file.write_text("blocked\n", encoding="utf-8")
    executor = LocalDockerExecutor(
        workspace_root=workspace_file,
        command_timeout=30,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args=kwargs.get("args", []), returncode=0, stdout="", stderr=""),
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "project": type(
                "ProjectStub",
                (),
                {
                    "branch": "main",
                    "repo_url": "https://github.com/example/private-repo",
                    "git_auth_type": "none",
                },
            )(),
        },
    )()

    with pytest.raises(WorkerExecutionError) as exc_info:
        executor.clone_repo(deployment)

    assert exc_info.value.step == "repository.workspace"
    assert "Unable to prepare deployment workspace" in exc_info.value.message
    assert exc_info.value.metadata["workspace_root"] == str(workspace_file)


def test_local_docker_executor_redacts_git_token_from_clone_logs(tmp_path, monkeypatch):
    token = "unit-test-github-token"

    def runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=f"auth failed for {token}\n")

    monkeypatch.setenv("CONTROL_PLANE_GIT_TOKEN_DEMO_APP", token)

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=runner,
        retry_count=0,
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "project": type(
                "ProjectStub",
                (),
                {
                    "branch": "main",
                    "repo_url": "https://github.com/example/private-repo",
                    "git_auth_type": "token",
                    "git_secret_ref": "DEMO_APP",
                },
            )(),
        },
    )()

    with pytest.raises(Exception):
        executor.clone_repo(deployment)

    log_contents = (tmp_path / "project-2" / "deployment-1" / "logs" / "clone.log").read_text(encoding="utf-8")
    assert token not in log_contents
    assert "***" in log_contents


def test_local_docker_executor_injects_secret_env_values_without_logging_them(tmp_path):
    commands = []
    secret_value = "postgres://user:super-secret@db/app"

    def runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        commands.append(args)
        if args[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=f"booted with {secret_value}\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=runner,
        port_allocator=lambda: 18080,
        health_probe=lambda _url: {"status_code": 200, "summary": secret_value},
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"image_tag": "demo-app:abc123def456"})(),
            "project": type(
                "ProjectStub",
                (),
                {
                    "name": "demo-app",
                    "port": 5000,
                    "healthcheck_path": "/health",
                    "env_vars": [
                        {"name": "APP_ENV", "value": "production"},
                        {"name": "DATABASE_URL", "value": secret_value, "is_secret": True},
                    ],
                },
            )(),
        },
    )()

    result = executor.deploy(deployment)

    run_command = next(command for command in commands if command[:3] == ["docker", "run", "--detach"])
    assert f"DATABASE_URL={secret_value}" in run_command
    deploy_log = (tmp_path / "project-2" / "deployment-1" / "logs" / "deploy.log").read_text(encoding="utf-8")
    runtime_log = (tmp_path / "project-2" / "deployment-1" / "logs" / "runtime.log").read_text(encoding="utf-8")
    assert secret_value not in deploy_log
    assert secret_value not in runtime_log
    assert "***" in deploy_log
    assert "[REDACTED]" in runtime_log
    assert result.metadata["runtime_log_summary"] == "booted with [REDACTED]"
    assert result.metadata["healthcheck_summary"] == "[REDACTED]"


def test_local_docker_executor_retries_host_port_collision(tmp_path):
    run_attempts = []
    ports = iter((18080, 18081))

    def runner(args, capture_output, text, timeout, check, input=None, env=None, heartbeat_cb=None, heartbeat_interval_seconds=None):
        if args[:3] == ["docker", "run", "--detach"]:
            run_attempts.append(args)
            if len(run_attempts) == 1:
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=1,
                    stdout="",
                    stderr="Bind for 127.0.0.1:18080 failed: port is already allocated\n",
                )
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="container123\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="ready\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    executor = LocalDockerExecutor(
        workspace_root=tmp_path,
        command_timeout=30,
        runner=runner,
        port_allocator=lambda: next(ports),
        health_probe=lambda _url: {"status_code": 200, "summary": "ok"},
    )
    deployment = type(
        "DeploymentStub",
        (),
        {
            "id": 1,
            "project_id": 2,
            "build": type("BuildStub", (), {"image_tag": "demo:abc"})(),
            "project": type(
                "ProjectStub",
                (),
                {"name": "demo", "port": 5000, "healthcheck_path": "/health", "env_vars": []},
            )(),
        },
    )()

    result = executor.deploy(deployment)

    assert len(run_attempts) == 2
    assert "127.0.0.1:18080:5000" in run_attempts[0]
    assert "127.0.0.1:18081:5000" in run_attempts[1]
    assert result.host_port == 18081
    assert result.metadata["port_allocation_attempts"] == 2
