"""Kubernetes objects for one app in one environment, plus the per-cluster objects the platform needs."""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from types import MappingProxyType

from berth.model import Override, Service
from berth.render.pod import LABEL_APP, LABEL_ENV, SERVICE_CONTAINER, Context, Doc, container, pod_labels, pod_spec, task_job

_NONE: Mapping[str, str] = MappingProxyType[str, str]({})
NO_PRUNE = {"kustomize.toolkit.fluxcd.io/prune": "disabled"}  # objects holding data are never deleted because a file went missing
POD_LAUNCHER = "berth-pod-launcher"
JOB_LAUNCHER = "berth-job-launcher"


class RenderError(ValueError):
    pass


def _meta(ctx: Context, name: str, *, component: str | None = None, annotations: Mapping[str, str] = _NONE) -> Doc:
    meta: Doc = {"name": name, "namespace": ctx.instance.namespace, "labels": ctx.labels(component)}
    if annotations:
        meta["annotations"] = dict(annotations)
    return meta


def instance_objects(ctx: Context, callers: AbstractSet[str]) -> list[Doc]:
    instance, platform = ctx.instance, ctx.platform
    environment = platform.environments[instance.env]
    docs: list[Doc] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": instance.namespace,
                "labels": ctx.labels() | {"pod-security.kubernetes.io/enforce": "restricted"},
                "annotations": {f"{LABEL_APP}-owners": ", ".join(ctx.app.owners), f"{LABEL_APP}-source": ctx.app.source} | NO_PRUNE,
            },
        }
    ]
    sa_annotations: dict[str, str] = {}
    if instance.azure_identity:
        sa_annotations["azure.workload.identity/client-id"] = platform.identities[f"{instance.app}/{instance.env}"]
    docs.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(ctx, ctx.app.name, annotations=sa_annotations)})
    if ctx.config_map is not None:
        docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(ctx, ctx.config_map), "data": dict(instance.config)})
    if ctx.files_map is not None:
        docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(ctx, ctx.files_map), "data": dict(instance.files)})
    if instance.secrets:
        docs.append(_external_secret(ctx))
    for name in sorted(instance.services):
        docs.extend(_service_objects(ctx, name, instance.services[name]))
    for name in sorted(instance.workflows):
        workflow = instance.workflows[name]
        if workflow.orchestrator == "kubernetes":
            ((task_name, task),) = workflow.tasks.items()
            docs.append(
                {
                    "apiVersion": "batch/v1",
                    "kind": "CronJob",
                    "metadata": _meta(ctx, f"{ctx.app.name}-{name}", component=name),
                    "spec": {
                        "schedule": workflow.schedule,
                        "timeZone": workflow.timezone,
                        "concurrencyPolicy": "Forbid",
                        "jobTemplate": {"spec": task_job(ctx, name, task_name, task, retries=task.retries)},
                    },
                }
            )
    orchestrators = {workflow.orchestrator for workflow in instance.workflows.values()}
    if "airflow" in orchestrators:
        docs.append(_launcher_binding(ctx, "airflow", POD_LAUNCHER, environment.airflow.namespace, environment.airflow.service_account))
    if "controlm" in orchestrators:
        docs.append(_launcher_binding(ctx, "controlm", JOB_LAUNCHER, environment.controlm.agent_namespace, environment.controlm.agent_service_account))
    docs.append(_network_policy(ctx, callers, environment.ingress_namespace))
    return _apply_overrides(docs, instance.overrides)


def _external_secret(ctx: Context) -> Doc:
    instance = ctx.instance
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": _meta(ctx, ctx.secret),
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": f"berth-{instance.env}"},
            "target": {"name": ctx.secret, "creationPolicy": "Owner"},
            # Key Vault names are flat, so each app's secrets live under its own prefix: <app>--<secret>.
            "data": [{"secretKey": secret, "remoteRef": {"key": f"{instance.app}--{secret}"}} for secret in instance.secrets],
        },
    }


def _service_objects(ctx: Context, name: str, service: Service) -> list[Doc]:
    instance, platform = ctx.instance, ctx.platform
    environment = platform.environments[instance.env]
    full = f"{ctx.app.name}-{name}"
    docs: list[Doc] = []
    mounts: list[Doc] = []
    volumes: list[Doc] = []
    for volume_name in sorted(service.volumes):
        volume = service.volumes[volume_name]
        claim = f"{full}-{volume_name}"
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": _meta(ctx, claim, component=name, annotations=NO_PRUNE),
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": platform.storage_class, "resources": {"requests": {"storage": f"{volume.size_gi}Gi"}}},
            }
        )
        mounts.append({"name": volume_name, "mountPath": volume.path})
        volumes.append({"name": volume_name, "persistentVolumeClaim": {"claimName": claim}})

    main = container(ctx, SERVICE_CONTAINER, service, mounts)
    if service.port is not None:
        main["ports"] = [{"name": "http", "containerPort": service.port}]
        probe: Doc = {"httpGet": {"path": service.health.path, "port": "http"}} if service.health is not None else {"tcpSocket": {"port": "http"}}
        main["readinessProbe"] = probe | {"periodSeconds": 5, "failureThreshold": 3}
        main["livenessProbe"] = probe | {"periodSeconds": 10, "failureThreshold": 6, "initialDelaySeconds": 10}
    spec = pod_spec(ctx, [main], extra_volumes=volumes, restart_policy=None)
    if environment.production:
        spec["topologySpreadConstraints"] = [{"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone", "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": ctx.selector(name)}}]
    # A bad version stalls the rollout instead of taking capacity away: old pods stay until new ones are ready.
    strategy: Doc = {"type": "Recreate"} if service.volumes else {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": "25%"}}
    deployment_spec: Doc = {
        "revisionHistoryLimit": 3,
        "progressDeadlineSeconds": 600,
        "strategy": strategy,
        "selector": {"matchLabels": ctx.selector(name)},
        "template": {"metadata": {"labels": pod_labels(ctx, name)}, "spec": spec},
    }
    if service.replicas is not None:
        deployment_spec = {"replicas": service.replicas} | deployment_spec
    docs.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": _meta(ctx, full, component=name), "spec": deployment_spec})

    if service.port is not None:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": _meta(ctx, name, component=name),
                "spec": {"selector": ctx.selector(name), "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
            }
        )
    if service.autoscale is not None:
        docs.append(
            {
                "apiVersion": "autoscaling/v2",
                "kind": "HorizontalPodAutoscaler",
                "metadata": _meta(ctx, full, component=name),
                "spec": {
                    "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": full},
                    "minReplicas": service.autoscale.min,
                    "maxReplicas": service.autoscale.max,
                    "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": service.autoscale.cpu_percent}}}],
                },
            }
        )
    floor = service.autoscale.min if service.autoscale is not None else (service.replicas or 0)
    if environment.production and floor >= 2:  # noqa: PLR2004 - a budget of one is only meaningful with two or more pods
        docs.append(
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": _meta(ctx, full, component=name), "spec": {"maxUnavailable": 1, "selector": {"matchLabels": ctx.selector(name)}}}
        )
    if service.expose == "public" and service.hostname is not None:
        docs.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "Ingress",
                "metadata": _meta(ctx, full, component=name),
                "spec": {
                    "ingressClassName": environment.ingress_class,
                    "rules": [
                        {
                            "host": f"{service.hostname}.{environment.public_domain}",
                            "http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {"service": {"name": name, "port": {"name": "http"}}}}]},
                        }
                    ],
                },
            }
        )
    return docs


def _launcher_binding(ctx: Context, scheduler: str, role: str, namespace: str, service_account: str) -> Doc:
    """Let one scheduler start pods or jobs in this namespace only, rather than cluster-wide."""
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": _meta(ctx, f"berth-{scheduler}"),
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": [{"kind": "ServiceAccount", "name": service_account, "namespace": namespace}],
    }


def _network_policy(ctx: Context, callers: AbstractSet[str], ingress_namespace: str) -> Doc:
    sources: list[Doc] = [{"podSelector": {}}]
    sources.extend({"namespaceSelector": {"matchLabels": {LABEL_APP: caller, LABEL_ENV: ctx.instance.env}}} for caller in sorted(callers))
    if any(service.expose == "public" for service in ctx.instance.services.values()):
        sources.append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ingress_namespace}}})
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _meta(ctx, "berth-ingress"),
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": sources}]},
    }


def merge_patch(target: object, patch: object) -> object:
    """RFC 7386: mappings merge, null deletes, anything else (lists included) replaces."""
    if not isinstance(patch, Mapping):
        return patch
    result: dict[str, object] = {str(key): value for key, value in target.items()} if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(str(key), None)
        else:
            result[str(key)] = merge_patch(result.get(str(key)), value)
    return result


def _as_doc(value: object, override: Override) -> Doc:
    if not isinstance(value, Mapping):
        msg = f"override for {override.target.kind}/{override.target.name} must leave a mapping"
        raise RenderError(msg)
    return {str(key): item for key, item in value.items()}


def _apply_overrides(docs: list[Doc], overrides: tuple[Override, ...]) -> list[Doc]:
    patched = list(docs)
    for override in overrides:
        matches = [
            index for index, doc in enumerate(patched) if doc.get("kind") == override.target.kind and isinstance(meta := doc.get("metadata"), Mapping) and meta.get("name") == override.target.name
        ]
        if not matches:
            msg = f"override target {override.target.kind}/{override.target.name} is not rendered for this app"
            raise RenderError(msg)
        patched[matches[0]] = _as_doc(merge_patch(patched[matches[0]], override.patch), override)
    return patched


def flux_kustomization(ctx: Context, cluster: str) -> Doc:
    namespace = ctx.instance.namespace
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": namespace, "namespace": "flux-system", "labels": ctx.labels()},
        "spec": {
            "interval": "10m",
            "retryInterval": "1m",
            "timeout": "5m",
            "path": f"./rendered/k8s/{cluster}/{namespace}",
            "prune": True,
            "wait": True,
            "sourceRef": {"kind": "GitRepository", "name": "berth"},
            "dependsOn": [{"name": "berth-platform"}],
        },
    }


def cluster_objects(envs: Mapping[str, tuple[str, str]]) -> list[Doc]:
    """Objects shared by every app on one cluster. `envs` maps each environment on the cluster to (key vault, tenant)."""
    docs: list[Doc] = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": POD_LAUNCHER, "labels": {"app.kubernetes.io/managed-by": "berth"}},
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "list", "watch", "delete", "patch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": JOB_LAUNCHER, "labels": {"app.kubernetes.io/managed-by": "berth"}},
            "rules": [
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch", "delete"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ],
        },
    ]
    for env in sorted(envs):
        vault, tenant = envs[env]
        docs.append(
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ClusterSecretStore",
                "metadata": {"name": f"berth-{env}", "labels": {"app.kubernetes.io/managed-by": "berth"}},
                "spec": {
                    "provider": {
                        "azurekv": {
                            "authType": "WorkloadIdentity",
                            "vaultUrl": f"https://{vault}.vault.azure.net",
                            "tenantId": tenant,
                            "serviceAccountRef": {"name": "external-secrets", "namespace": "external-secrets"},
                        }
                    },
                    # Only namespaces of this environment may read this environment's vault.
                    "conditions": [{"namespaceSelector": {"matchLabels": {LABEL_ENV: env}}}],
                },
            }
        )
    return docs
