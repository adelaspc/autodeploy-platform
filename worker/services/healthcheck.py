import time
from urllib.error import HTTPError
from urllib.request import urlopen

from control_plane.security import redact_text
from worker.execution.contracts import WorkerExecutionError


class DockerHealthcheckServiceMixin:
    def _wait_for_healthcheck(self, healthcheck_url, log_path, *, redacted_values=()):
        deadline = time.monotonic() + self.healthcheck_timeout
        attempts = 0
        last_error = None
        last_summary = None
        last_heartbeat = time.monotonic()

        while time.monotonic() < deadline:
            last_heartbeat = self._heartbeat_if_due(last_heartbeat)
            attempts += 1
            try:
                probe_result = self.health_probe(healthcheck_url)
            except Exception as exc:
                last_error = redact_text(str(exc), secret_values=redacted_values)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"Healthcheck attempt {attempts} failed: {last_error}\n")
                self.sleep_fn(self.healthcheck_interval)
                continue

            summary = redact_text(probe_result["summary"], secret_values=redacted_values)
            metadata = {
                "healthcheck_attempts": attempts,
                "healthcheck_status_code": probe_result["status_code"],
                "healthcheck_summary": summary,
            }
            if 200 <= probe_result["status_code"] < 400:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"Healthcheck attempt {attempts} succeeded: {probe_result['status_code']} {summary}\n"
                    )
                return metadata

            last_error = f"HTTP {probe_result['status_code']}"
            last_summary = summary
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"Healthcheck attempt {attempts} failed: {probe_result['status_code']} {summary}\n")
            self.sleep_fn(self.healthcheck_interval)

        raise WorkerExecutionError(
            "deploy.healthcheck",
            f"Healthcheck did not succeed within {self.healthcheck_timeout} seconds",
            metadata={
                "healthcheck_attempts": attempts,
                "healthcheck_last_error": last_error,
                "healthcheck_last_summary": last_summary,
                "healthcheck_url": healthcheck_url,
            },
            log_path=str(log_path),
        )

    @staticmethod
    def _default_health_probe(url):
        try:
            with urlopen(url, timeout=5) as response:  # nosec B310
                body = response.read(512).decode("utf-8", errors="replace")
                return {
                    "status_code": response.status,
                    "summary": body.strip()[:200] or f"HTTP {response.status}",
                }
        except HTTPError as exc:
            body = exc.read(512).decode("utf-8", errors="replace")
            return {
                "status_code": exc.code,
                "summary": body.strip()[:200] or f"HTTP {exc.code}",
            }
