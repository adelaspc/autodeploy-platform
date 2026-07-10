from urllib.error import HTTPError, URLError

from backend.api.live_health import deployment_healthcheck_url, probe_deployment_health


class ResponseStub:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status


def deployment_stub(*, status="running", service_url="http://demo.example", healthcheck_path="/health"):
    project = type("ProjectStub", (), {"healthcheck_path": healthcheck_path})()
    return type("DeploymentStub", (), {"status": status, "service_url": service_url, "project": project})()


def test_deployment_healthcheck_url_uses_recorded_service_and_project_path():
    deployment = deployment_stub(service_url="https://demo.example/", healthcheck_path="/ready")

    assert deployment_healthcheck_url(deployment) == "https://demo.example/ready"


def test_live_health_reports_successful_http_probe():
    result = probe_deployment_health(
        deployment_stub(),
        opener=lambda request, timeout: ResponseStub(200),
    )

    assert result["status"] == "healthy"
    assert result["http_status"] == 200
    assert result["checked_at"]


def test_live_health_reports_http_and_network_failures():
    def http_failure(_request, timeout):
        raise HTTPError("http://demo.example/health", 503, "Unavailable", {}, None)

    def network_failure(_request, timeout):
        raise URLError("connection refused")

    http_result = probe_deployment_health(deployment_stub(), opener=http_failure)
    network_result = probe_deployment_health(deployment_stub(), opener=network_failure)

    assert http_result["status"] == "unhealthy"
    assert http_result["http_status"] == 503
    assert network_result["status"] == "unhealthy"
    assert network_result["http_status"] is None


def test_live_health_skips_non_running_or_non_public_deployments():
    stopped = probe_deployment_health(deployment_stub(status="stopped"))
    internal = probe_deployment_health(deployment_stub(service_url="demo.default.svc.cluster.local"))

    assert stopped["status"] == "unavailable"
    assert internal["status"] == "unavailable"
