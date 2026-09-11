"""Simulate the deployment lifecycle without creating runtime infrastructure."""

from control_plane.deployment_spec import project_for_deployment
from worker.execution.contracts import DeploymentExecutor, ExecutionResult, ExecutorContract, PreflightResult
from worker.workload_environment import resolved_workload_environment


class FakeDeploymentExecutor(DeploymentExecutor):
    deploy_target = "fake"
    contract = ExecutorContract(
        name="fake",
        deploy_target="fake",
        runtime="simulated",
        healthcheck_strategy="simulated",
    )

    def clone_repo(self, deployment):
        return ExecutionResult(
            "Repository cloned",
            metadata={"executor": self.deploy_target},
            workspace_path=f"/tmp/paas-workspaces/fake/deployment-{deployment.id}",
        )

    def build_image(self, deployment):
        project = project_for_deployment(deployment)
        image_tag = deployment.build.image_tag or deployment.build.commit_sha[:12]
        image_name = deployment.build.image_name or project.name
        image_ref = deployment.build.image_ref or f"local/{image_name}:{image_tag}"
        return ExecutionResult(
            "Docker image built",
            metadata={"executor": self.deploy_target},
            image_tag=image_tag,
            image_ref=image_ref,
        )

    def run_tests(self, deployment):
        return ExecutionResult("Test command completed", metadata={"executor": self.deploy_target})

    def tag_image(self, deployment):
        project = project_for_deployment(deployment)
        return ExecutionResult(
            "Image tag skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            image_tag=deployment.build.image_tag or f"{project.name}:{deployment.id}",
            image_ref=deployment.build.image_ref or f"{project.name}:{deployment.id}",
        )

    def push_image(self, deployment):
        return ExecutionResult(
            "Image push skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            image_tag=deployment.build.image_tag,
            image_ref=deployment.build.image_ref or deployment.build.image_tag,
        )

    def verify_image(self, deployment):
        return ExecutionResult(
            "Image verification skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            image_tag=deployment.build.image_tag,
            image_ref=deployment.build.image_ref or deployment.build.image_tag,
        )

    def preflight_deploy(self, deployment):
        return PreflightResult(
            status="skipped",
            summary="Deployment preflight skipped for fake executor",
            metadata={"executor": self.deploy_target, "skipped": True},
            deploy_target=self.deploy_target,
        )

    def deploy(self, deployment):
        project = project_for_deployment(deployment)
        return ExecutionResult(
            "Deployment marked as running",
            metadata={
                "executor": self.deploy_target,
                "platform_environment_names": [item["name"] for item in resolved_workload_environment(deployment)],
            },
            service_url=deployment.service_url or f"https://{project.name}.local",
            deploy_target=self.deploy_target,
        )

    def stop(self, deployment):
        return ExecutionResult(
            "Deployment marked as stopped",
            metadata={"executor": self.deploy_target, "stopped": True},
            deploy_target=self.deploy_target,
            container_name=deployment.container_name,
            container_id=deployment.container_id,
        )

    def cleanup_workspace(self, deployment):
        return {"workspace_removed": False, "log_removed": False}
