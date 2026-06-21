from types import SimpleNamespace

import pytest

from worker.helm_runner import HelmCommandError, HelmResult, HelmRunner


def make_runner(*, returncode=0, stdout="ok\n", stderr=""):
    calls = []

    def fake_runner(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    fake_runner.calls = calls
    return fake_runner


def test_upgrade_install_command_args():
    runner = HelmRunner(namespace="apps", chart_path="./deploy/helm/generic-web-app", helm_timeout="240s")

    assert runner.upgrade_install_args("release-name", "/tmp/values.yaml") == [
        "helm",
        "upgrade",
        "--install",
        "release-name",
        "./deploy/helm/generic-web-app",
        "--namespace",
        "apps",
        "--create-namespace",
        "-f",
        "/tmp/values.yaml",
        "--wait",
        "--timeout",
        "240s",
    ]


def test_uninstall_command_args():
    runner = HelmRunner(namespace="apps", helm_timeout="90s")

    assert runner.uninstall_args("release-name") == [
        "helm",
        "uninstall",
        "release-name",
        "--namespace",
        "apps",
        "--wait",
        "--timeout",
        "90s",
    ]


def test_status_command_args():
    runner = HelmRunner(namespace="apps")

    assert runner.status_args("release-name") == [
        "helm",
        "status",
        "release-name",
        "--namespace",
        "apps",
        "--output",
        "json",
    ]


def test_custom_helm_binary_is_used():
    runner = HelmRunner(namespace="apps", helm_binary="/usr/local/bin/helm", chart_path="chart")

    assert runner.upgrade_install_args("release-name", "values.yaml")[0] == "/usr/local/bin/helm"
    assert runner.uninstall_args("release-name")[0] == "/usr/local/bin/helm"
    assert runner.status_args("release-name")[0] == "/usr/local/bin/helm"


def test_runner_call_kwargs_and_success_result():
    fake_runner = make_runner(stdout="deployed\n", stderr="warning\n")
    runner = HelmRunner(
        namespace="apps",
        chart_path="chart",
        runner=fake_runner,
        env={"HELM_CACHE_HOME": "/tmp/helm-cache"},
    )

    result = runner.upgrade_install("release-name", "values.yaml")

    assert isinstance(result, HelmResult)
    assert result.returncode == 0
    assert result.stdout == "deployed\n"
    assert result.stderr == "warning\n"
    assert result.args == runner.upgrade_install_args("release-name", "values.yaml")
    assert fake_runner.calls == [
        {
            "args": result.args,
            "kwargs": {
                "capture_output": True,
                "text": True,
                "check": False,
                "env": {"HELM_CACHE_HOME": "/tmp/helm-cache"},
            },
        }
    ]
    assert "shell" not in fake_runner.calls[0]["kwargs"]


def test_failed_helm_command_raises_error_with_result():
    fake_runner = make_runner(returncode=1, stdout="", stderr="release failed\n")
    runner = HelmRunner(namespace="apps", chart_path="chart", runner=fake_runner)

    with pytest.raises(HelmCommandError) as exc_info:
        runner.upgrade_install("release-name", "values.yaml")

    assert exc_info.value.result.returncode == 1
    assert exc_info.value.result.stderr == "release failed\n"
    assert "release failed" in str(exc_info.value)


def test_status_and_uninstall_do_not_require_chart_path():
    fake_runner = make_runner(stdout='{"name":"release-name"}\n')
    runner = HelmRunner(namespace="apps", runner=fake_runner)

    status = runner.status("release-name")
    uninstall = runner.uninstall("release-name")

    assert status.args == runner.status_args("release-name")
    assert uninstall.args == runner.uninstall_args("release-name")


def test_upgrade_install_fails_clearly_without_chart_path():
    runner = HelmRunner(namespace="apps")

    with pytest.raises(ValueError, match="chart_path is required"):
        runner.upgrade_install_args("release-name", "values.yaml")


def test_empty_namespace_is_rejected():
    with pytest.raises(ValueError, match="namespace is required"):
        HelmRunner(namespace=" ")


def test_upgrade_install_requires_values_file():
    runner = HelmRunner(namespace="apps", chart_path="chart")

    with pytest.raises(ValueError, match="values_file is required"):
        runner.upgrade_install_args("release-name", "")


def test_release_name_is_required_for_all_commands():
    runner = HelmRunner(namespace="apps", chart_path="chart")

    with pytest.raises(ValueError, match="release is required"):
        runner.upgrade_install_args("", "values.yaml")
    with pytest.raises(ValueError, match="release is required"):
        runner.uninstall_args("")
    with pytest.raises(ValueError, match="release is required"):
        runner.status_args("")
