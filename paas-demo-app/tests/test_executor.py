from pathlib import Path
import subprocess

from worker.executor import LocalDockerExecutor
from worker.service import process_next_pending_deployment

from tests.test_worker import create_pending_deployment


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

    assert [command[:2] for command in commands[:2]] == [["git", "clone"], ["docker", "build"]]
    assert commands[2][:3] == ["docker", "run", "--rm"]
    assert commands[3][:3] == ["docker", "rm", "--force"]
    assert commands[4][:3] == ["docker", "run", "--detach"]
    assert commands[5][:2] == ["docker", "logs"]

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

    assert build_result.image_tag == "demo-app:abc123def456"
    assert build_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert tag_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert push_result.image_ref == "registry.example.com/paas/demo-app:abc123def456"
    assert any(item["args"][:2] == ["docker", "tag"] for item in commands)
    assert any(item["args"][:2] == ["docker", "push"] for item in commands)
    login_call = next(item for item in commands if item["args"][:2] == ["docker", "login"])
    assert login_call["input"] == "super-secret"
    assert "super-secret" not in " ".join(" ".join(item["args"]) for item in commands)


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

    assert tag_result.metadata["skipped"] is True
    assert push_result.metadata["skipped"] is True


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
        {"id": 1, "project_id": 2, "project": type("ProjectStub", (), {"branch": "main", "repo_url": "/tmp/repo"})()},
    )()

    result = executor.clone_repo(deployment)

    assert attempts["count"] == 2
    assert result.metadata["attempt"] == 2
    assert result.metadata["total_attempts"] == 2
    log_contents = (tmp_path / "project-2" / "deployment-1" / "logs" / "clone.log").read_text(encoding="utf-8")
    assert "==== retry ====" in log_contents
