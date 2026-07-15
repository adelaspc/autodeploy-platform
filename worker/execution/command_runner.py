import shlex
import subprocess
import time
from datetime import datetime, timezone

from control_plane.security import redact_sensitive_data
from worker.execution.contracts import ExecutionResult, WorkerExecutionError


class CommandExecutionMixin:
    def _run_command(self, step, args, *, log_path, stdin_input=None, env=None, redacted_values=None):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        total_attempts = 1 + self.retry_count if step in self.retryable_steps else 1
        last_error = None
        redacted_values = tuple(value for value in (redacted_values or []) if value)

        for attempt in range(1, total_attempts + 1):
            started_at = datetime.now(timezone.utc)
            try:
                completed = self._execute_command(args, stdin_input=stdin_input, env=env)
            except subprocess.TimeoutExpired as exc:
                output = self._sanitize_text((exc.stdout or "") + (exc.stderr or ""), redacted_values=redacted_values)
                metadata = self._build_command_metadata(
                    args=args,
                    output=output,
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    timed_out=True,
                    redacted_values=redacted_values,
                )
                self._write_log(log_path, args, output, metadata=metadata, append=attempt > 1, redacted_values=redacted_values)
                last_error = WorkerExecutionError(
                    step,
                    f"Command timed out after {self.command_timeout} seconds",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            except OSError as exc:
                output = self._sanitize_text(str(exc), redacted_values=redacted_values)
                metadata = self._build_command_metadata(
                    args=args,
                    output=output,
                    started_at=started_at,
                    returncode=None,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    redacted_values=redacted_values,
                )
                self._write_log(log_path, args, output, metadata=metadata, append=attempt > 1, redacted_values=redacted_values)
                last_error = WorkerExecutionError(
                    step,
                    f"Command execution failed: {exc}",
                    metadata=metadata,
                    log_path=str(log_path),
                )
            else:
                combined_output = self._sanitize_text(
                    (completed.stdout or "") + (completed.stderr or ""),
                    redacted_values=redacted_values,
                )
                metadata = self._build_command_metadata(
                    args=args,
                    output=combined_output,
                    started_at=started_at,
                    returncode=completed.returncode,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    redacted_values=redacted_values,
                )
                self._write_log(
                    log_path,
                    args,
                    combined_output,
                    metadata=metadata,
                    append=attempt > 1,
                    redacted_values=redacted_values,
                )
                if completed.returncode == 0:
                    message = self._summarize_output(combined_output) or f"{step} completed successfully"
                    return ExecutionResult(message, metadata=metadata, log_path=str(log_path))

                last_error = WorkerExecutionError(
                    step,
                    metadata["summary"] or f"Command failed with exit code {completed.returncode}",
                    metadata=metadata,
                    log_path=str(log_path),
                )

            if attempt < total_attempts:
                self.sleep_fn(min(attempt, 3))

        raise last_error

    def _execute_command(self, args, *, allow_heartbeat=True, stdin_input=None, env=None):
        heartbeat_cb = self.heartbeat if allow_heartbeat else None
        if self.runner is not None:
            try:
                return self.runner(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout,
                    check=False,
                    input=stdin_input,
                    env=env,
                    heartbeat_cb=heartbeat_cb,
                    heartbeat_interval_seconds=self.heartbeat_interval,
                )
            except TypeError:
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                        env=env,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                        heartbeat_cb=heartbeat_cb,
                        heartbeat_interval_seconds=self.heartbeat_interval,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        input=stdin_input,
                    )
                except TypeError:
                    pass
                try:
                    return self.runner(
                        args,
                        capture_output=True,
                        text=True,
                        timeout=self.command_timeout,
                        check=False,
                        heartbeat_cb=heartbeat_cb,
                        heartbeat_interval_seconds=self.heartbeat_interval,
                    )
                except TypeError:
                    pass
                return self.runner(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout,
                    check=False,
                )

        if stdin_input is not None:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
                input=stdin_input,
                env=env,
            )

        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        deadline = time.monotonic() + self.command_timeout
        last_heartbeat = time.monotonic()

        while True:
            returncode = process.poll()
            if returncode is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)

            now = time.monotonic()
            if now >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(args, self.command_timeout, output=stdout, stderr=stderr)

            try:
                last_heartbeat = self._heartbeat_if_due(last_heartbeat, heartbeat=heartbeat_cb)
            except Exception:
                process.kill()
                process.wait(timeout=5)
                raise

            self.sleep_fn(min(0.5, max(0.05, deadline - now)))

    def _heartbeat_if_due(self, last_heartbeat, *, heartbeat=None):
        heartbeat = self.heartbeat if heartbeat is None else heartbeat
        if heartbeat is None:
            return last_heartbeat
        now = time.monotonic()
        if now - last_heartbeat >= self.heartbeat_interval:
            heartbeat()
            return now
        return last_heartbeat

    @staticmethod
    def _summarize_output(output):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            return None
        if len(lines) == 1:
            return lines[0][:500]
        return f"{lines[-2][:200]} | {lines[-1][:200]}"

    @staticmethod
    def _tail_lines(output, *, limit=10, line_width=240):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return [line[:line_width] for line in lines[-limit:]]

    def _build_command_metadata(
        self,
        *,
        args,
        output,
        started_at,
        returncode,
        attempt,
        total_attempts,
        timed_out=False,
        redacted_values=None,
    ):
        finished_at = datetime.now(timezone.utc)
        return {
            "command": self._sanitize_args(args, redacted_values=redacted_values),
            "returncode": returncode,
            "attempt": attempt,
            "total_attempts": total_attempts,
            "timed_out": timed_out,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
            "summary": self._summarize_output(output),
            "output_tail": self._tail_lines(output),
        }

    def _write_log(self, log_path, args, output, *, metadata=None, append=False, redacted_values=None):
        sanitized_args = self._sanitize_args(args, redacted_values=redacted_values)
        rendered = ["Command:", " ".join(shlex.quote(part) for part in sanitized_args)]
        if metadata:
            rendered.extend(
                [
                    "",
                    "Metadata:",
                    f"attempt={metadata.get('attempt')}/{metadata.get('total_attempts')}",
                    f"returncode={metadata.get('returncode')}",
                    f"timed_out={metadata.get('timed_out')}",
                    f"duration_seconds={metadata.get('duration_seconds')}",
                ]
            )
        rendered.extend(["", "Output:", output])
        payload = "\n".join(rendered) + "\n"
        mode = "a" if append else "w"
        with log_path.open(mode, encoding="utf-8") as handle:
            if append:
                handle.write("\n==== retry ====\n")
            handle.write(payload)

    @staticmethod
    def _sanitize_text(text, *, redacted_values=None):
        sanitized = text
        for value in redacted_values or ():
            if value:
                sanitized = sanitized.replace(value, "***")
        return sanitized

    def _sanitize_args(self, args, *, redacted_values=None):
        return [self._sanitize_text(str(part), redacted_values=redacted_values) for part in args]

    @staticmethod
    def _event(event_type, status, message, *, step=None, level="info", metadata=None):
        return {
            "event_type": event_type,
            "status": status,
            "message": message,
            "step": step,
            "level": level,
            "metadata_json": redact_sensitive_data(metadata),
        }

