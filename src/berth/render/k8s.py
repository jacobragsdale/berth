"""Kubernetes objects for one app in one environment, plus the per-cluster objects the platform needs."""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from types import MappingProxyType

from berth.model import Override, Platform, Service, Software
from berth.render.pod import LABEL_APP, LABEL_ENV, SERVICE_CONTAINER, Context, Doc, container, datadog_labels, pod_labels, pod_spec, task_job, telemetry_service, writable_volumes

_NONE: Mapping[str, str] = MappingProxyType[str, str]({})
GROUP = "berth.contoso.io"
# berth's prune never deletes an object carrying this, so data is never lost because a file went missing.
KEEP = {f"{GROUP}/keep": "true"}
MANAGED = {"app.kubernetes.io/managed-by": "berth"}
PLATFORM_LABEL = f"{GROUP}/platform"
POD_LAUNCHER = "berth-pod-launcher"
JOB_LAUNCHER = "berth-job-launcher"
GATEWAY = "berth"  # the GatewayClass and the one Gateway in each cluster
PLATFORM_STORE = "berth-platform"
PORT_NAME = "app"


class RenderError(ValueError):
    pass


def _meta(ctx: Context, name: str, *, component: str | None = None, annotations: Mapping[str, str] = _NONE) -> Doc:
    meta: Doc = {"name": name, "namespace": ctx.instance.namespace, "labels": ctx.labels(component)}
    if annotations:
        meta["annotations"] = dict(annotations)
    return meta


def links(platform: Platform, app: str, env: str, component: str) -> dict[str, str]:
    """The platform's link templates filled in for one workload, e.g. its logs in Datadog."""
    fields = {"app": app, "env": env, "namespace": f"{app}-{env}", "service": telemetry_service(app, component), "cluster": platform.environments[env].cluster}
    return {name: template.format_map(fields) for name, template in sorted(platform.links.items())}


def _workload_meta(ctx: Context, name: str, component: str) -> Doc:
    meta = _meta(ctx, name, component=component)
    meta["labels"] = ctx.labels(component) | datadog_labels(ctx, component)
    return meta


def _binding(ctx: Context, name: str, role: str, subjects: list[Doc]) -> Doc:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": _meta(ctx, name),
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role},
        "subjects": subjects,
    }


def instance_objects(ctx: Context, callers: AbstractSet[str]) -> list[Doc]:
    instance, platform = ctx.instance, ctx.platform
    environment = platform.environments[instance.env]
    annotations = {f"{LABEL_APP}-owners": ", ".join(ctx.app.owners), f"{LABEL_APP}-source": ctx.app.source}
    if ctx.app.calls:
        annotations[f"{LABEL_APP}-calls"] = ", ".join(ctx.app.calls)
    docs: list[Doc] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": instance.namespace, "labels": ctx.labels() | {"pod-security.kubernetes.io/enforce": "restricted"}, "annotations": annotations | KEEP},
        }
    ]
    sa_annotations: dict[str, str] = {}
    if instance.azure_identity:
        sa_annotations["azure.workload.identity/client-id"] = platform.identities[f"{instance.app}/{instance.env}"]
    docs.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(ctx, ctx.app.name, annotations=sa_annotations)})
    # Owners can read everything in their namespaces except Secrets, including pod logs, with their own kubectl login.
    docs.append(_binding(ctx, "berth-owners", "view", [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": owner} for owner in ctx.app.owners]))
    if platform.kubectl_writers:
        # Break-glass: these groups may change anything in the namespace. The daily drift report shows what they changed.
        docs.append(_binding(ctx, "berth-writers", "edit", [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": group} for group in platform.kubectl_writers]))
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
                    "metadata": _workload_meta(ctx, f"{ctx.app.name}-{name}", f"{name}-{task_name}"),
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
    docs.append(_network_policy(ctx, callers, platform.gateway.namespace))
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
                "metadata": _meta(ctx, claim, component=name, annotations=KEEP),
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": platform.storage_class, "resources": {"requests": {"storage": f"{volume.size_gi}Gi"}}},
            }
        )
        mounts.append({"name": volume_name, "mountPath": volume.path})
        volumes.append({"name": volume_name, "persistentVolumeClaim": {"claimName": claim}})

    main = container(ctx, SERVICE_CONTAINER, name, service, mounts)
    if service.port is not None:
        main["ports"] = [{"name": PORT_NAME, "containerPort": service.port}]
        probe: Doc = {"httpGet": {"path": service.health.path, "port": PORT_NAME}} if service.health is not None else {"tcpSocket": {"port": PORT_NAME}}
        main["readinessProbe"] = probe | {"periodSeconds": 5, "failureThreshold": 3}
        main["livenessProbe"] = probe | {"periodSeconds": 10, "failureThreshold": 6, "initialDelaySeconds": 10}
    spec = pod_spec(ctx, [main], extra_volumes=volumes + writable_volumes(service), restart_policy=None)
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
    docs.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": _workload_meta(ctx, full, name), "spec": deployment_spec})

    if service.port is not None:
        docs.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": _meta(ctx, name, component=name),
                "spec": {"selector": ctx.selector(name), "ports": [{"name": PORT_NAME, "port": service.port, "targetPort": PORT_NAME}]},
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
    if service.expose == "company" and service.hostname is not None and service.port is not None:
        docs.append(
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": _meta(ctx, full, component=name),
                "spec": {
                    # The cluster's Gateway has one listener per environment, which admits routes only from that
                    # environment's namespaces, so a dev route cannot claim a qa hostname.
                    "parentRefs": [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "namespace": platform.gateway.namespace, "name": GATEWAY, "sectionName": f"https-{instance.env}"}],
                    "hostnames": [f"{service.hostname}.{environment.domain}"],
                    "rules": [{"backendRefs": [{"name": name, "port": service.port}]}],
                },
            }
        )
    return docs


def _launcher_binding(ctx: Context, scheduler: str, role: str, namespace: str, service_account: str) -> Doc:
    """Let one scheduler start pods or jobs in this namespace only, rather than cluster-wide."""
    return _binding(ctx, f"berth-{scheduler}", role, [{"kind": "ServiceAccount", "name": service_account, "namespace": namespace}])


def _network_policy(ctx: Context, callers: AbstractSet[str], gateway_namespace: str) -> Doc:
    sources: list[Doc] = [{"podSelector": {}}]
    sources.extend({"namespaceSelector": {"matchLabels": {LABEL_APP: caller, LABEL_ENV: ctx.instance.env}}} for caller in sorted(callers))
    if any(service.expose == "company" for service in ctx.instance.services.values()):
        sources.append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": gateway_namespace}}})
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


def _store(name: str, vault: str, tenant: str, condition: Doc) -> Doc:
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ClusterSecretStore",
        "metadata": {"name": name, "labels": MANAGED},
        "spec": {
            "provider": {
                "azurekv": {
                    "authType": "WorkloadIdentity",
                    "vaultUrl": f"https://{vault}.vault.azure.net",
                    "tenantId": tenant,
                    "serviceAccountRef": {"name": "external-secrets", "namespace": "external-secrets"},
                }
            },
            "conditions": [condition],
        },
    }


def gateway_certificates(platform: Platform, cluster: str) -> list[str]:
    """The TLS Secrets the cluster's Gateway expects in the gateway namespace: a wildcard per environment."""
    return [f"berth-{env}-tls" for env in sorted(platform.envs_on(cluster))]


def _gateway_objects(platform: Platform, cluster: str) -> list[Doc]:
    namespace = platform.gateway.namespace
    listeners: list[Doc] = [
        {
            "name": f"https-{env}",
            "protocol": "HTTPS",
            "port": 443,
            "hostname": f"*.{platform.environments[env].domain}",
            "tls": {"mode": "Terminate", "certificateRefs": [{"kind": "Secret", "name": f"berth-{env}-tls"}]},
            "allowedRoutes": {"kinds": [{"kind": "HTTPRoute"}], "namespaces": {"from": "Selector", "selector": {"matchLabels": MANAGED | {LABEL_ENV: env}}}},
        }
        for env in sorted(platform.envs_on(cluster))
    ]
    listeners.append({"name": "http", "protocol": "HTTP", "port": 80, "allowedRoutes": {"namespaces": {"from": "Same"}}})
    gateway_class: Doc = {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "GatewayClass", "metadata": {"name": GATEWAY, "labels": MANAGED}}
    gateway_class["spec"] = {"controllerName": platform.gateway.controller}
    # A private Azure load balancer on the reserved address, which the environments' wildcard DNS records point at.
    infrastructure = {"annotations": {"service.beta.kubernetes.io/azure-load-balancer-internal": "true", "service.beta.kubernetes.io/azure-load-balancer-ipv4": platform.clusters[cluster].gateway_ip}}
    gateway: Doc = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": GATEWAY, "namespace": namespace, "labels": MANAGED},
        "spec": {"gatewayClassName": GATEWAY, "infrastructure": infrastructure, "listeners": listeners},
    }
    redirect: Doc = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": "https-redirect", "namespace": namespace, "labels": MANAGED},
        "spec": {"parentRefs": [{"name": GATEWAY, "sectionName": "http"}], "rules": [{"filters": [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}]}]},
    }
    return [gateway_class, gateway, redirect]


def _software_secrets(platform: Platform, cluster: str, software: Mapping[str, Software]) -> list[Doc]:
    docs: list[Doc] = []
    namespaces: set[str] = set()
    for software_name in sorted(software):
        for release, install in sorted(software[software_name].installs.items()):
            if install.cluster != cluster:
                continue
            for secret_name, secret in sorted(install.secrets.items()):
                namespaces.add(install.namespace)
                target: Doc = {"name": secret_name, "creationPolicy": "Owner"}
                if secret.type != "Opaque":
                    target["template"] = {"type": secret.type}
                docs.append(
                    {
                        "apiVersion": "external-secrets.io/v1",
                        "kind": "ExternalSecret",
                        "metadata": {"name": secret_name, "namespace": install.namespace, "labels": MANAGED | {"app.kubernetes.io/instance": release}},
                        "spec": {
                            "refreshInterval": "1h",
                            "secretStoreRef": {"kind": "ClusterSecretStore", "name": PLATFORM_STORE},
                            "target": target,
                            "data": [{"secretKey": key, "remoteRef": {"key": vault_key}} for key, vault_key in sorted(secret.data.items())],
                        },
                    }
                )
    if not docs:
        return []
    # Only the software namespaces that declare secrets may read the platform vault; apps never can.
    return [_store(PLATFORM_STORE, platform.clusters[cluster].key_vault, platform.tenant_id, {"namespaces": sorted(namespaces)}), *docs]


def software_namespaces(cluster: str, software: Mapping[str, Software]) -> list[Doc]:
    """Namespaces for platform software, created before its charts are applied. Never pruned."""
    names = sorted({install.namespace for item in software.values() for install in item.installs.values() if install.cluster == cluster})
    # Not labelled as berth's: app admission policies and app drift checks do not apply to platform software.
    return [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {PLATFORM_LABEL: "true"}, "annotations": dict(KEEP)}} for name in names]


def cluster_objects(platform: Platform, cluster: str, software: Mapping[str, Software]) -> list[Doc]:
    """Objects shared by every app on one cluster. Applied after platform software, before any app."""
    docs: list[Doc] = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": POD_LAUNCHER, "labels": MANAGED},
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["create", "get", "list", "watch", "delete", "patch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["list"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": JOB_LAUNCHER, "labels": MANAGED},
            "rules": [
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch", "delete"]},
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
            ],
        },
    ]
    # Only namespaces of an environment may read that environment's vault.
    docs += [_store(f"berth-{env}", platform.environments[env].key_vault, platform.tenant_id, {"namespaceSelector": {"matchLabels": {LABEL_ENV: env}}}) for env in sorted(platform.envs_on(cluster))]
    return docs + _gateway_objects(platform, cluster) + _software_secrets(platform, cluster, software)
