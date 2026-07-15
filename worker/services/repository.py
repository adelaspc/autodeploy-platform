import base64
import os
import shutil

from worker.execution.contracts import WorkerExecutionError


class RepositoryServiceMixin:
    def clone_repo(self, deployment):
        workspace_dir, repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "clone.log"

        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        clone_args = [
            "git",
            "clone",
            "--branch",
            deployment.project.branch,
            "--single-branch",
            deployment.project.repo_url,
            str(repo_dir),
        ]
        clone_env, redacted_values = self._git_clone_environment(deployment)
        result = self._run_command(
            "repository.clone",
            clone_args,
            log_path=log_path,
            env=clone_env,
            redacted_values=redacted_values,
        )
        result.workspace_path = str(workspace_dir)
        return result

    def _git_clone_environment(self, deployment):
        git_auth_type = (getattr(deployment.project, "git_auth_type", None) or "none").strip().lower()
        if git_auth_type == "none":
            return None, ()
        if git_auth_type != "token":
            raise WorkerExecutionError("repository.clone", f"Unsupported git auth type '{git_auth_type}'")

        secret_ref = (getattr(deployment.project, "git_secret_ref", None) or "").strip()
        if not secret_ref:
            raise WorkerExecutionError("repository.clone", "git_secret_ref is required when git_auth_type is 'token'")

        token_env_name = f"CONTROL_PLANE_GIT_TOKEN_{secret_ref}"
        token = os.getenv(token_env_name)
        if not token:
            raise WorkerExecutionError(
                "repository.clone",
                f"Git token environment variable '{token_env_name}' is not set",
            )

        repo_url = deployment.project.repo_url or ""
        if not repo_url.startswith("https://github.com/"):
            raise WorkerExecutionError(
                "repository.clone",
                "Token-based git auth currently supports only https://github.com/ repository URLs",
            )

        auth_header = "AUTHORIZATION: basic " + base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraheader",
                "GIT_CONFIG_VALUE_0": auth_header,
            }
        )
        return env, (token, auth_header)
