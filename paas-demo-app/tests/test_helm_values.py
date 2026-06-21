from types import SimpleNamespace

from worker.helm_values import GenericWebAppValuesConfig, generic_web_app_values


def make_deployment(
    *,
    image_ref="registry.example.com/team/app:abc123",
    registry=None,
    image_name=None,
    image_tag=None,
    port=8080,
    healthcheck_path="/health",
    env_vars=None,
    cpu=None,
    memory=None,
    default_test_command=None,
    test_command=None,
    migration_command=None,
):
    project = SimpleNamespace(
        name="demo-app",
        port=port,
        healthcheck_path=healthcheck_path,
        env_vars=env_vars or [],
        cpu=cpu,
        memory=memory,
        default_test_command=default_test_command,
        migration_command=migration_command,
    )
    build = SimpleNamespace(
        image_ref=image_ref,
        registry=registry,
        image_name=image_name,
        image_tag=image_tag,
        test_command=test_command,
    )
    return SimpleNamespace(project=project, build=build)


def test_minimal_config_produces_generic_web_app_values():
    values = generic_web_app_values(
        make_deployment(),
        GenericWebAppValuesConfig(image_pull_secret="registry-pull-secret"),
    )

    assert values["image"] == {
        "repository": "registry.example.com/team/app",
        "tag": "abc123",
        "pullPolicy": "IfNotPresent",
        "pullSecrets": [{"name": "registry-pull-secret"}],
    }
    assert values["replicaCount"] == 1
    assert values["container"] == {"port": 8080}
    assert values["env"] == []
    assert values["envFrom"] == {"configMaps": [], "secrets": []}
    assert values["service"]["type"] == "ClusterIP"
    assert values["resources"] == {}


def test_image_ref_with_registry_port_parses_correctly():
    values = generic_web_app_values(make_deployment(image_ref="localhost:32000/my-app:dev"))

    assert values["image"]["repository"] == "localhost:32000/my-app"
    assert values["image"]["tag"] == "dev"


def test_fallback_image_fields_are_used_when_image_ref_is_unavailable():
    values = generic_web_app_values(
        make_deployment(
            image_ref=None,
            registry="localhost:32000",
            image_name="fallback-app",
            image_tag="build-42",
        )
    )

    assert values["image"]["repository"] == "localhost:32000/fallback-app"
    assert values["image"]["tag"] == "build-42"


def test_custom_project_port_maps_to_container_and_service_port():
    values = generic_web_app_values(make_deployment(port=3000))

    assert values["container"]["port"] == 3000
    assert values["service"]["port"] == 3000


def test_literal_env_vars_map_to_env_values():
    values = generic_web_app_values(
        make_deployment(
            env_vars=[
                {"name": "APP_ENV", "value": "production"},
                {"name": "FEATURE_FLAG", "value_source": "literal", "value": True},
            ]
        )
    )

    assert values["env"] == [
        {"name": "APP_ENV", "value": "production"},
        {"name": "FEATURE_FLAG", "value": "True"},
    ]


def test_configmap_key_references_map_to_value_from():
    values = generic_web_app_values(
        make_deployment(
            env_vars=[
                {
                    "name": "APP_ENV",
                    "value_source": "configmap_key_ref",
                    "source_name": "app-config",
                    "source_key": "app-env",
                }
            ]
        )
    )

    assert values["env"] == [
        {
            "name": "APP_ENV",
            "valueFrom": {
                "configMapKeyRef": {
                    "name": "app-config",
                    "key": "app-env",
                }
            },
        }
    ]
    assert values["envFrom"] == {"configMaps": [], "secrets": []}


def test_secret_key_references_map_to_value_from():
    values = generic_web_app_values(
        make_deployment(
            env_vars=[
                {
                    "name": "DATABASE_URL",
                    "value_source": "secret_key_ref",
                    "source_name": "app-secret",
                    "source_key": "database-url",
                }
            ]
        )
    )

    assert values["env"] == [
        {
            "name": "DATABASE_URL",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "app-secret",
                    "key": "database-url",
                }
            },
        }
    ]
    assert values["envFrom"] == {"configMaps": [], "secrets": []}


def test_healthcheck_path_maps_to_readiness_and_liveness_probe_paths():
    values = generic_web_app_values(make_deployment(healthcheck_path="/ready"))

    assert values["probes"]["readiness"]["enabled"] is True
    assert values["probes"]["readiness"]["httpGet"] == {"path": "/ready", "port": "http"}
    assert values["probes"]["liveness"]["enabled"] is True
    assert values["probes"]["liveness"]["httpGet"] == {"path": "/ready", "port": "http"}


def test_startup_probe_is_disabled_by_default():
    values = generic_web_app_values(make_deployment())

    assert values["probes"]["startup"] == {"enabled": False}


def test_ingress_is_disabled_by_default():
    values = generic_web_app_values(make_deployment())

    assert values["ingress"] == {"enabled": False}


def test_service_target_port_is_empty_so_chart_default_applies():
    values = generic_web_app_values(make_deployment(port=5000))

    assert values["service"]["targetPort"] == ""


def test_command_and_args_are_not_generated_from_test_or_migration_commands():
    values = generic_web_app_values(
        make_deployment(
            default_test_command="pytest -q",
            test_command="npm test",
            migration_command="flask db upgrade",
        )
    )

    assert "command" not in values["container"]
    assert "args" not in values["container"]


def test_resource_requests_map_from_project_cpu_and_memory():
    values = generic_web_app_values(make_deployment(cpu="250m", memory="256Mi"))

    assert values["resources"] == {"requests": {"cpu": "250m", "memory": "256Mi"}}
