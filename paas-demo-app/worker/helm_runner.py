import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HelmResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class HelmCommandError(RuntimeError):
    def __init__(self, result: HelmResult):
        super().__init__(f"Helm command failed with exit code {result.returncode}: {result.stderr}")
        self.result = result


class HelmRunner:
    def __init__(
        self,
        *,
        namespace: str,
        helm_binary: str = "helm",
        chart_path: str | None = None,
        helm_timeout: str = "180s",
        runner=subprocess.run,
        env: dict[str, str] | None = None,
    ):
        if not str(namespace or "").strip():
            raise ValueError("namespace is required")
        self.namespace = str(namespace).strip()
        self.helm_binary = str(helm_binary or "helm").strip() or "helm"
        self.chart_path = str(chart_path).strip() if chart_path is not None else None
        self.helm_timeout = str(helm_timeout or "180s").strip() or "180s"
        self.runner = runner
        self.env = env

    def upgrade_install_args(self, release: str, values_file: str):
        release = _required_string(release, "release")
        values_file = _required_string(values_file, "values_file")
        chart_path = _required_string(self.chart_path, "chart_path")
        return [
            self.helm_binary,
            "upgrade",
            "--install",
            release,
            chart_path,
            "--namespace",
            self.namespace,
            "--create-namespace",
            "-f",
            values_file,
            "--wait",
            "--timeout",
            self.helm_timeout,
        ]

    def uninstall_args(self, release: str):
        release = _required_string(release, "release")
        return [
            self.helm_binary,
            "uninstall",
            release,
            "--namespace",
            self.namespace,
            "--wait",
            "--timeout",
            self.helm_timeout,
        ]

    def status_args(self, release: str):
        release = _required_string(release, "release")
        return [
            self.helm_binary,
            "status",
            release,
            "--namespace",
            self.namespace,
            "--output",
            "json",
        ]

    def upgrade_install(self, release: str, values_file: str) -> HelmResult:
        return self._run(self.upgrade_install_args(release, values_file))

    def uninstall(self, release: str) -> HelmResult:
        return self._run(self.uninstall_args(release))

    def status(self, release: str) -> HelmResult:
        return self._run(self.status_args(release))

    def _run(self, args: list[str]) -> HelmResult:
        completed = self.runner(
            args,
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
        )
        result = HelmResult(
            args=list(args),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if result.returncode != 0:
            raise HelmCommandError(result)
        return result


def _required_string(value, field_name):
    if not str(value or "").strip():
        raise ValueError(f"{field_name} is required")
    return str(value).strip()
