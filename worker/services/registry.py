from worker.execution.contracts import ExecutionResult, WorkerExecutionError


class RegistryServiceMixin:
    def tag_image(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "tag.log"
        local_image_tag = deployment.build.image_tag
        registry_image_ref = deployment.build.image_ref
        if not local_image_tag:
            raise WorkerExecutionError("image.tag", "Local image tag is missing before registry tag step")
        if not self.registry_enabled:
            log_path.write_text("Registry push disabled. Tag step skipped.\n", encoding="utf-8")
            return ExecutionResult(
                "Image tag skipped because registry push is disabled",
                metadata={"executor": self.deploy_target, "skipped": True, "registry_enabled": False},
                log_path=str(log_path),
                image_tag=local_image_tag,
                image_ref=registry_image_ref or local_image_tag,
            )
        if not registry_image_ref:
            raise WorkerExecutionError("image.tag", "Registry image reference is missing before push step")

        result = self._run_command(
            "image.tag",
            ["docker", "tag", local_image_tag, registry_image_ref],
            log_path=log_path,
        )
        result.image_tag = local_image_tag
        result.image_ref = registry_image_ref
        return result

    def push_image(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "push.log"
        if not self.registry_enabled:
            log_path.write_text("Push skipped because registry push is disabled.\n", encoding="utf-8")
            return ExecutionResult(
                "Image push skipped because registry push is disabled",
                metadata={"executor": self.deploy_target, "skipped": True, "registry_enabled": False},
                log_path=str(log_path),
                image_tag=deployment.build.image_tag,
                image_ref=deployment.build.image_ref or deployment.build.image_tag,
            )

        registry_image_ref = deployment.build.image_ref
        if not registry_image_ref:
            raise WorkerExecutionError("image.push", "Registry image reference is missing before push step")

        login_metadata = {}
        if self.registry_username and self.registry_password:
            login_result = self._run_command(
                "image.login",
                ["docker", "login", self.registry_url, "--username", self.registry_username, "--password-stdin"],
                log_path=log_path,
                stdin_input=self.registry_password,
            )
            login_metadata = {"login_summary": login_result.message}

        push_result = self._run_command("image.push", ["docker", "push", registry_image_ref], log_path=log_path)
        push_result.image_tag = deployment.build.image_tag
        push_result.image_ref = registry_image_ref
        push_result.metadata |= login_metadata
        return push_result

    def verify_image(self, deployment):
        _workspace_dir, _repo_dir, logs_dir = self._prepare_workspace(deployment)
        log_path = logs_dir / "verify-image.log"
        registry_image_ref = deployment.build.image_ref
        if not self.registry_enabled:
            log_path.write_text("Image verification skipped because registry push is disabled.\n", encoding="utf-8")
            return ExecutionResult(
                "Image verification skipped because registry push is disabled",
                metadata={"executor": self.deploy_target, "skipped": True, "registry_enabled": False},
                log_path=str(log_path),
                image_tag=deployment.build.image_tag,
                image_ref=registry_image_ref or deployment.build.image_tag,
            )
        if not registry_image_ref:
            raise WorkerExecutionError("image.verify", "Registry image reference is missing before verification step")

        verify_result = self._run_command(
            "image.verify",
            ["docker", "buildx", "imagetools", "inspect", registry_image_ref],
            log_path=log_path,
        )
        verify_result.image_tag = deployment.build.image_tag
        verify_result.image_ref = registry_image_ref
        return verify_result

    def _registry_image_ref(self, image_name, tag_suffix):
        if not self.registry_enabled:
            return None
        if not self.registry_url:
            raise WorkerExecutionError("image.tag", "Registry is enabled but CONTROL_PLANE_REGISTRY_URL is not configured")
        namespace = f"{self.registry_namespace}/" if self.registry_namespace else ""
        return f"{self.registry_url}/{namespace}{image_name}:{tag_suffix}"
