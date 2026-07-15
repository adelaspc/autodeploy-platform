import subprocess

from worker.execution.contracts import WorkerExecutionError


class PortForwardHealthcheckMixin:
    def _port_forward_healthcheck(self, deployment, *, service_name, log_path):
        local_port = self.port_allocator()
        probe_url = f"http://127.0.0.1:{local_port}{deployment.project.healthcheck_path}"
        command = self._kubectl_args(
            "port-forward",
            f"service/{service_name}",
            f"{local_port}:{deployment.project.port}",
            "--address",
            "127.0.0.1",
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8")
        process = self.popen_factory(command, stdout=handle, stderr=subprocess.STDOUT, text=True)
        try:
            self.sleep_fn(0.2)
            metadata = self._wait_for_healthcheck(probe_url, log_path)
            metadata["port_forward_local_port"] = local_port
            metadata["port_forward_log_path"] = str(log_path)
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


