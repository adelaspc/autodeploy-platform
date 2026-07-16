from control_plane.deployment_spec import project_for_deployment
from control_plane.security import env_var_is_secret
from worker.execution.contracts import WorkerExecutionError


class KubernetesManifestRendererMixin:
    def _manifest(self, deployment, *, deployment_name, service_name):
        project = project_for_deployment(deployment)
        labels = {
            "app.kubernetes.io/name": self._sanitize_image_component(project.name),
            "app.kubernetes.io/managed-by": "autodeploy-control-plane",
            "app.kubernetes.io/instance": deployment_name,
        }
        literal_secret_names = [
            item.get("name")
            for item in project.env_vars or []
            if isinstance(item, dict) and env_var_is_secret(item) and item.get("value") is not None
        ]
        if literal_secret_names:
            raise WorkerExecutionError(
                "deploy.kubernetes.manifest",
                "Kubernetes deployments require secret_key_ref for secret env vars",
                metadata={"secret_env_var_names": sorted(str(name) for name in literal_secret_names if name)},
            )
        env = self._kubernetes_env_vars(project.env_vars)

        pod_spec = {
            "automountServiceAccountToken": False,
            "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [
                {
                    "name": "app",
                    "image": deployment.build.image_ref,
                    "ports": [{"containerPort": project.port}],
                    "env": env,
                    "securityContext": {"allowPrivilegeEscalation": False},
                }
            ]
        }
        if self.image_pull_secret:
            pod_spec["imagePullSecrets"] = [{"name": self.image_pull_secret}]

        items = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": deployment_name, "namespace": self.namespace, "labels": labels},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": labels},
                    "template": {"metadata": {"labels": labels}, "spec": pod_spec},
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": service_name, "namespace": self.namespace, "labels": labels},
                "spec": {
                    "selector": labels,
                    "ports": [
                        {
                            "name": "http",
                            "port": project.port,
                            "targetPort": project.port,
                        }
                    ],
                    "type": "ClusterIP",
                },
            },
        ]
        if self.ingress_enabled:
            ingress_spec = {
                "rules": [
                    {
                        "host": self._ingress_host(deployment.id),
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": service_name,
                                            "port": {"number": project.port},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
            if self.ingress_class_name:
                ingress_spec["ingressClassName"] = self.ingress_class_name
            items.append(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": {"name": deployment_name, "namespace": self.namespace, "labels": labels},
                    "spec": ingress_spec,
                }
            )

        return {"apiVersion": "v1", "kind": "List", "items": items}

    @staticmethod
    def _kubernetes_env_vars(env_vars):
        rendered = []
        for item in env_vars or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            value_source = item.get("value_source")
            if value_source is None:
                value_source = "literal" if item.get("value") is not None else None
            if value_source == "literal" and item.get("value") is not None:
                rendered.append({"name": name, "value": str(item["value"])})
                continue
            if value_source == "configmap_key_ref" and item.get("source_name") and item.get("source_key"):
                rendered.append(
                    {
                        "name": name,
                        "valueFrom": {
                            "configMapKeyRef": {"name": str(item["source_name"]), "key": str(item["source_key"])}
                        },
                    }
                )
                continue
            if value_source == "secret_key_ref" and item.get("source_name") and item.get("source_key"):
                rendered.append(
                    {
                        "name": name,
                        "valueFrom": {
                            "secretKeyRef": {"name": str(item["source_name"]), "key": str(item["source_key"])}
                        },
                    }
                )
        return rendered

    @staticmethod
    def _kubernetes_referenced_resources(env_vars):
        configmaps = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "configmap_key_ref"
                and item.get("source_name")
            }
        )
        secrets = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "secret_key_ref"
                and item.get("source_name")
            }
        )
        return {"configmaps": configmaps, "secrets": secrets}

    @staticmethod
    def _env_source_summary(env_vars):
        configmap_refs = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "configmap_key_ref"
                and item.get("source_name")
            }
        )
        secret_refs = sorted(
            {
                str(item.get("source_name"))
                for item in env_vars or []
                if isinstance(item, dict)
                and item.get("value_source") == "secret_key_ref"
                and item.get("source_name")
            }
        )
        literal_count = sum(
            1
            for item in env_vars or []
            if isinstance(item, dict)
            and (
                item.get("value_source") == "literal"
                or (item.get("value_source") is None and item.get("value") is not None)
            )
        )
        return {
            "env_var_count": len([item for item in env_vars or [] if isinstance(item, dict) and item.get("name")]),
            "literal_env_count": literal_count,
            "configmap_refs_used": configmap_refs,
            "secret_refs_used": secret_refs,
        }
