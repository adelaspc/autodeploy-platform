"""Verify Kubernetes Services from the worker through a temporary port-forward."""

import subprocess

from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import WorkerExecutionError


class PortForwardHealthcheckMixin:
    def _port_forward_healthcheck(self, deployment, *, service_name, log_path):
        project = project_for_deployment(deployment)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for port_attempt in range(1, 4):
            local_port = self.port_allocator()
            probe_url = f"http://127.0.0.1:{local_port}{project.healthcheck_path}"
            command = self._kubectl_args(
                "port-forward",
                f"service/{service_name}",
                f"{local_port}:{project.port}",
                "--address",
                "127.0.0.1",
            )
            handle = log_path.open("w", encoding="utf-8")
            process = self.popen_factory(command, stdout=handle, stderr=subprocess.STDOUT, text=True)
            try:
                self.sleep_fn(0.2)
                if process.poll() is not None:
                    handle.flush()
                    if port_attempt < 3 and self._port_forward_bind_failed(log_path):
                        continue
                    raise WorkerExecutionError(
                        "deploy.kubernetes.healthcheck",
                        "Kubernetes port-forward exited before healthcheck started",
                        metadata={"port_forward_local_port": local_port, "port_forward_log_path": str(log_path)},
                        log_path=str(log_path),
                    )
                metadata = self._wait_for_healthcheck(probe_url, log_path)
                metadata["port_forward_local_port"] = local_port
                metadata["port_forward_log_path"] = str(log_path)
                metadata["port_forward_attempts"] = port_attempt
                return metadata
            except WorkerExecutionError as exc:
                raise WorkerExecutionError(
                    exc.step,
                    exc.message,
                    metadata=exc.metadata | {"port_forward_local_port": local_port, "port_forward_log_path": str(log_path)},
                    log_path=exc.log_path,
                ) from exc
            finally:
                self._terminate_process(process)
                handle.close()

    @staticmethod
    def _port_forward_bind_failed(log_path):
        output = log_path.read_text(encoding="utf-8", errors="replace").lower()
        return any(marker in output for marker in ("address already in use", "unable to listen", "bind:"))
