"""The pod every workload gets, whichever system starts it: a Deployment, a CronJob, Control-M or Airflow."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from berth.fleet import Instance
from berth.model import App, Platform, Service, Size, Task, duration_seconds

GROUP = "berth.contoso.io"  # the prefix of every label and annotation berth owns
LABEL_APP = f"{GROUP}/app"
LABEL_ENV = f"{GROUP}/env"
MANAGED = {"app.kubernetes.io/managed-by": "berth"}  # on every object berth renders; what admission and prune select
TASK_CONTAINER = "base"  # KubernetesPodOperator finds its container by this name
SERVICE_CONTAINER = "main"
RUN_AS = 10001

type Doc = dict[str, object]


def content_hash(data: Mapping[str, str]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:10]


def env_var_name(*parts: str) -> str:
    return "_".join(part.upper().replace("-", "_") for part in parts)


@dataclass(frozen=True)
class Context:
    app: App
    instance: Instance
    platform: Platform
    ports: Mapping[str, int] = field(default_factory=dict)  # the port of each `talks_to` service, by `app/service`

    @property
    def image(self) -> str:
        return f"{self.platform.registry}/{self.app.repository}:{self.instance.version}"

    @property
    def config_map(self) -> str | None:
        config = self.instance.config
        return f"{self.app.name}-config-{content_hash(config)}" if config else None

    @property
    def files_map(self) -> str | None:
        files = self.instance.files
        return f"{self.app.name}-files-{content_hash(files)}" if files else None

    @property
    def secret(self) -> str:
        return f"{self.app.name}-secrets"

    def labels(self, component: str | None = None) -> dict[str, str]:
        # No version label here: it would change every object on every release. Only pods carry it.
        labels = {"app.kubernetes.io/name": self.app.name, "app.kubernetes.io/managed-by": "berth", LABEL_APP: self.app.name, LABEL_ENV: self.instance.env}
        if component is not None:
            labels["app.kubernetes.io/component"] = component
        return labels

    def selector(self, component: str) -> dict[str, str]:
        return {"app.kubernetes.io/name": self.app.name, "app.kubernetes.io/component": component}

    def size(self, name: str) -> Size:
        return self.platform.sizes[name]


def service_host(app: str, service: str, env: str) -> str:
    return f"{service}.{app}-{env}.svc.cluster.local"


# Never through the proxy: the cluster's own names, and always localhost.
_LOCAL = ("localhost", "127.0.0.1", ".svc", ".cluster.local")


def proxy_env(platform: Platform, env: str) -> list[Doc]:
    environment = platform.environments[env]
    if environment.proxy is None:
        return []
    bypass = ",".join(dict.fromkeys([*_LOCAL, *platform.clusters[environment.cluster].networks, f".{environment.domain}", *environment.proxy.no_proxy]))
    url = environment.proxy.url
    return [{"name": name, "value": value} for name, value in (("HTTP_PROXY", url), ("HTTPS_PROXY", url), ("NO_PROXY", bypass), ("http_proxy", url), ("https_proxy", url), ("no_proxy", bypass))]


def telemetry_service(app: str, component: str) -> str:
    """The service name Datadog and OpenTelemetry see: the same as the Deployment or CronJob name."""
    return f"{app}-{component}"


def _env(ctx: Context, component: str) -> list[Doc]:
    instance = ctx.instance
    env: list[Doc] = [{"name": "BERTH_APP", "value": instance.app}, {"name": "BERTH_ENV", "value": instance.env}, {"name": "BERTH_VERSION", "value": instance.tag}]
    # One source for the env, service and version tags, so traces, metrics and logs line up with what berth deployed.
    attributes = f"deployment.environment.name={instance.env},service.version={instance.tag},service.namespace={instance.app}"
    env += [{"name": "OTEL_SERVICE_NAME", "value": telemetry_service(instance.app, component)}, {"name": "OTEL_RESOURCE_ATTRIBUTES", "value": attributes}]
    endpoint = ctx.platform.environments[instance.env].otlp_endpoint
    if endpoint is not None:
        if "$(HOST_IP)" in endpoint:
            env.append({"name": "HOST_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.hostIP"}}})
        env.append({"name": "OTEL_EXPORTER_OTLP_ENDPOINT", "value": endpoint})
    env += proxy_env(ctx.platform, instance.env)
    env.extend({"name": env_var_name(secret), "valueFrom": {"secretKeyRef": {"name": ctx.secret, "key": secret}}} for secret in instance.secrets)
    for ref in instance.talks_to:
        app, service = ref.split("/", 1)
        host, port = service_host(app, service, instance.env), ctx.ports[ref]
        env += [
            {"name": env_var_name(app, service, "host"), "value": host},
            {"name": env_var_name(app, service, "port"), "value": str(port)},
            {"name": env_var_name(app, service, "url"), "value": f"http://{host}:{port}"},
        ]
    return env


def berth_variables(ctx: Context) -> set[str]:
    """The names of the variables berth sets on every container. A setting of the same name would silently lose to it,
    because a container's own `env` wins over the ConfigMap it reads settings from."""
    return {str(item["name"]) for item in _env(ctx, "")}


def resources(size: Size) -> Doc:
    # No CPU limit: throttling hurts latency more than a noisy neighbour does. Memory limit equals the request.
    return {"requests": {"cpu": size.cpu, "memory": size.memory}, "limits": {"memory": size.memory}}


def container(ctx: Context, name: str, component: str, runs: Service | Task, extra_mounts: list[Doc]) -> Doc:
    doc: Doc = {"name": name, "image": ctx.image}
    if runs.command is not None:
        doc["command"] = list(runs.command)
    if runs.args is not None:
        doc["args"] = list(runs.args)
    doc["env"] = _env(ctx, component)
    if ctx.config_map is not None:
        doc["envFrom"] = [{"configMapRef": {"name": ctx.config_map}}]
    doc["resources"] = resources(ctx.size(runs.size))
    doc["securityContext"] = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True, "runAsUser": RUN_AS, "capabilities": {"drop": ["ALL"]}}
    mounts: list[Doc] = [{"name": "tmp", "mountPath": "/tmp"}]  # noqa: S108 - an emptyDir, because the root filesystem is read-only
    if ctx.files_map is not None:
        mounts.append({"name": "files", "mountPath": "/config", "readOnly": True})
    mounts += [{"name": f"writable-{index}", "mountPath": path} for index, path in enumerate(runs.writable)]
    doc["volumeMounts"] = mounts + extra_mounts
    return doc


def writable_volumes(runs: Service | Task) -> list[Doc]:
    """Empty directories for the paths an image must write to, matching the mounts `container` adds."""
    return [{"name": f"writable-{index}", "emptyDir": {"sizeLimit": "1Gi"}} for index in range(len(runs.writable))]


def pod_spec(ctx: Context, containers: list[Doc], *, extra_volumes: list[Doc], restart_policy: str | None) -> Doc:
    volumes: list[Doc] = [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    if ctx.files_map is not None:
        volumes.append({"name": "files", "configMap": {"name": ctx.files_map}})
    spec: Doc = {
        "serviceAccountName": ctx.app.name,
        "automountServiceAccountToken": ctx.instance.azure_identity,  # workload identity needs the projected token
        "securityContext": {"runAsNonRoot": True, "runAsUser": RUN_AS, "fsGroup": RUN_AS, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": containers,
        "volumes": volumes + extra_volumes,
    }
    if restart_policy is not None:
        spec["restartPolicy"] = restart_policy
    return spec


def datadog_labels(ctx: Context, component: str) -> dict[str, str]:
    """Datadog unified service tagging, read from pod labels by the Datadog Agent."""
    return {"tags.datadoghq.com/env": ctx.instance.env, "tags.datadoghq.com/service": telemetry_service(ctx.app.name, component)}


def pod_labels(ctx: Context, component: str) -> dict[str, str]:
    labels = ctx.labels(component) | datadog_labels(ctx, component) | {"app.kubernetes.io/version": ctx.instance.tag, "tags.datadoghq.com/version": ctx.instance.tag}
    if ctx.instance.azure_identity:
        labels["azure.workload.identity/use"] = "true"
    return labels


def task_pod(ctx: Context, workflow: str, task_name: str, task: Task) -> Doc:
    component = f"{workflow}-{task_name}"
    main = container(ctx, TASK_CONTAINER, component, task, [])
    spec = pod_spec(ctx, [main], extra_volumes=writable_volumes(task), restart_policy="Never")
    return {"metadata": {"namespace": ctx.instance.namespace, "labels": pod_labels(ctx, component)}, "spec": spec}


def task_job(ctx: Context, workflow: str, task_name: str, task: Task, *, retries: int) -> Doc:
    """A batch/v1 Job spec. Kubernetes owns retries and the deadline, so the scheduler only starts and watches it."""
    return {"backoffLimit": retries, "activeDeadlineSeconds": duration_seconds(task.timeout), "ttlSecondsAfterFinished": 86400, "template": task_pod(ctx, workflow, task_name, task)}
