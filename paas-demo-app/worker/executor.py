class WorkerExecutionError(Exception):
    def __init__(self, step, message):
        super().__init__(message)
        self.step = step
        self.message = message


class FakeDeploymentExecutor:
    def clone_repository(self, deployment):
        return "Repository cloned"

    def build_image(self, deployment):
        return "Docker image built"

    def run_tests(self, deployment):
        return "Test command completed"

    def push_image(self, deployment):
        return "Image pushed to registry"

    def deploy_application(self, deployment):
        return deployment.service_url or f"https://{deployment.project.name}.local"
