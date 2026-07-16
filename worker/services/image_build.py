from control_plane.command_validation import CommandValidationError, parse_optional_command
from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import WorkerExecutionError


class ImageBuildServiceMixin:
    def build_image(self, deployment):
        project = project_for_deployment(deployment)
        workspace_dir, repo_dir, logs_dir = self._prepare_workspace(deployment)
        dockerfile_path = repo_dir / project.dockerfile_path
        build_context_path = repo_dir / project.build_context

        if not dockerfile_path.is_file():
            raise WorkerExecutionError(
                "image.build",
                f"Dockerfile not found at '{dockerfile_path}'",
                metadata={"dockerfile_path": str(dockerfile_path)},
            )

        if not build_context_path.exists():
            raise WorkerExecutionError(
                "image.build",
                f"Build context not found at '{build_context_path}'",
                metadata={"build_context": str(build_context_path)},
            )

        image_name = self._image_name(deployment)
        tag_suffix = self._tag_suffix(deployment)
        image_tag = f"{image_name}:{tag_suffix}"
        image_ref = self._registry_image_ref(image_name, tag_suffix) or image_tag
        log_path = logs_dir / "build.log"
        result = self._run_command(
            "image.build",
            [
                "docker",
                "build",
                "--tag",
                image_tag,
                "--file",
                str(dockerfile_path),
                str(build_context_path),
            ],
            log_path=log_path,
        )
        result.workspace_path = str(workspace_dir)
        result.image_tag = image_tag
        result.image_ref = image_ref
        result.metadata |= {
            "local_image_tag": image_tag,
            "registry_image_ref": image_ref if image_ref != image_tag else None,
        }
        return result

    def run_tests(self, deployment):
        workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "tests.log"
        try:
            test_command = parse_optional_command(deployment.build.test_command, "test_command")
        except CommandValidationError as exc:
            raise WorkerExecutionError("tests", str(exc), metadata={"test_command_valid": False}) from exc
        if not test_command:
            raise WorkerExecutionError("tests", "Configured test command is empty")

        result = self._run_command(
            "tests",
            ["docker", "run", "--rm", deployment.build.image_tag, *test_command],
            log_path=log_path,
        )
        result.workspace_path = str(workspace_dir)
        return result

    @staticmethod
    def _sanitize_image_component(value):
        sanitized = "".join(char.lower() if char.isalnum() or char in {"-", "_", "."} else "-" for char in value)
        return sanitized.strip("-") or "app"

    def _image_name(self, deployment):
        return self._sanitize_image_component(deployment.build.image_name or project_for_deployment(deployment).name)

    def _tag_suffix(self, deployment):
        raw = deployment.build.image_tag or deployment.build.commit_sha[:12] or str(deployment.id)
        if ":" in raw:
            raw = raw.rsplit(":", 1)[-1]
        return self._sanitize_image_component(raw)
