import base64
import os
import shutil

from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import WorkerExecutionError


class RepositoryServiceMixin:
    def clone_repo(self, deployment):
        project = project_for_deployment(deployment)
        workspace_dir, repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "clone.log"
        checkout_log_path = logs_dir / "checkout.log"

        if repo_dir.exists():
            shutil.rmtree(repo_dir)

        clone_args = [
            "git",
            "clone",
            "--branch",
            self._deployment_branch(deployment),
            "--single-branch",
            project.repo_url,
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
        commit_sha = (deployment.build.commit_sha or "").strip()
        if not commit_sha:
            raise WorkerExecutionError(
                "repository.checkout",
                "Deployment commit SHA is missing after repository clone",
            )
        checkout_result = self._run_command(
            "repository.checkout",
            ["git", "-C", str(repo_dir), "checkout", "--detach", commit_sha],
            log_path=checkout_log_path,
            env=clone_env,
            redacted_values=redacted_values,
        )
        result.workspace_path = str(workspace_dir)
        result.metadata |= {
            "branch": self._deployment_branch(deployment),
            "commit_sha": commit_sha,
            "checkout_log_path": str(checkout_log_path),
            "checkout_summary": checkout_result.message,
        }
        return result

    @staticmethod
    def _deployment_branch(deployment):
        project = project_for_deployment(deployment)
        if getattr(deployment, "spec_snapshot_json", None):
            return project.branch
        for event in getattr(deployment, "events", ()) or ():
            if getattr(event, "event_type", None) != "deployment.created":
                continue
            metadata = getattr(event, "metadata_json", None) or {}
            branch = metadata.get("branch") if isinstance(metadata, dict) else None
            if isinstance(branch, str) and branch.strip():
                return branch.strip()
        return project.branch

    def _git_clone_environment(self, deployment):
        project = project_for_deployment(deployment)
        git_auth_type = (getattr(project, "git_auth_type", None) or "none").strip().lower()
        if git_auth_type == "none":
            return None, ()
        if git_auth_type != "token":
            raise WorkerExecutionError("repository.clone", f"Unsupported git auth type '{git_auth_type}'")

        secret_ref = (getattr(project, "git_secret_ref", None) or "").strip()
        if not secret_ref:
            raise WorkerExecutionError("repository.clone", "git_secret_ref is required when git_auth_type is 'token'")

        token_env_name = f"CONTROL_PLANE_GIT_TOKEN_{secret_ref}"
        token = os.getenv(token_env_name)
        if not token:
            raise WorkerExecutionError(
                "repository.clone",
                f"Git token environment variable '{token_env_name}' is not set",
            )

        repo_url = project.repo_url or ""
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
