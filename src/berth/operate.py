"""Deploy, check drift, and break glass against one cluster. The decisions come from `berth.drift`; this module
gathers facts with `berth.kube` and reports in plain lines, so a pipeline log reads as a record of what happened."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TextIO

from berth import drift, yamlio
from berth.fleet import Fleet
from berth.kube import STAMP_MANAGER, Kubectl, KubeError, ObjectRef, Tools, helm_template
from berth.render import ROOT
from berth.render.k8s import MANAGED
from berth.render.pod import LABEL_APP, LABEL_ENV
from berth.render.software import releases, template_args, values

_SELECTOR = ",".join(f"{key}={value}" for key, value in MANAGED.items())
ROLLOUT_SECONDS = 600
# kubectl reports objects by Kind and resource types by plural name; these are the ones berth renders.
_KINDS = {
    "configmaps": "ConfigMap",
    "serviceaccounts": "ServiceAccount",
    "persistentvolumeclaims": "PersistentVolumeClaim",
    "services": "Service",
    "deployments.apps": "Deployment",
    "cronjobs.batch": "CronJob",
    "horizontalpodautoscalers.autoscaling": "HorizontalPodAutoscaler",
    "poddisruptionbudgets.policy": "PodDisruptionBudget",
    "networkpolicies.networking.k8s.io": "NetworkPolicy",
    "rolebindings.rbac.authorization.k8s.io": "RoleBinding",
    "externalsecrets.external-secrets.io": "ExternalSecret",
    "httproutes.gateway.networking.k8s.io": "HTTPRoute",
}


@dataclass(frozen=True)
class Target:
    app: str
    env: str
    namespace: str

    def path(self, cluster: str) -> str:
        return f"{ROOT}/k8s/{cluster}/{self.namespace}/manifests.yaml"


def targets(fleet: Fleet, cluster: str) -> list[Target]:
    return [Target(i.app, i.env, i.namespace) for i in fleet.instances() if fleet.platform.environments[i.env].cluster == cluster]


@dataclass(frozen=True)
class Selection:
    targets: tuple[Target, ...]
    platform: bool  # also apply platform software and _platform/


def select(all_targets: Iterable[Target], cluster: str, *, app: str | None, env: str | None, changed: Iterable[str] | None) -> Selection:
    """Which namespaces a deploy touches: one app, what changed since a ref, or everything."""
    chosen = list(all_targets)
    if app is not None:
        chosen = [t for t in chosen if t.app == app and (env is None or t.env == env)]
        return Selection(tuple(chosen), platform=False)
    if changed is None:
        return Selection(tuple(chosen), platform=True)
    paths = set(changed)
    platform = any(path.startswith((f"{ROOT}/k8s/{cluster}/_platform/", "platform/software/")) for path in paths)
    return Selection(tuple(t for t in chosen if t.path(cluster) in paths), platform=platform)


@dataclass(frozen=True)
class Site:
    """One cluster, as berth sees it: the specs, their rendered files, and how to reach the cluster."""

    fleet: Fleet
    tree: dict[str, str]
    kubectl: Kubectl
    tools: Tools
    cluster: str

    def manifests(self, target: Target) -> str:
        return self.tree[target.path(self.cluster)]

    def rendered(self, target: Target) -> list[dict[str, object]]:
        path = target.path(self.cluster)
        return [{str(k): v for k, v in doc.items()} for doc in yamlio.load_all(self.tree[path], source=path) if isinstance(doc, dict)]

    def types(self, wanted: Iterable[str]) -> list[str]:
        known = self.kubectl.resource_types()
        return [name for name in wanted if name in known]

    def namespaces(self) -> dict[str, ObjectRef]:
        return {ref.name: ref for ref in self.kubectl.namespaces(_SELECTOR)}


@dataclass(frozen=True)
class DeployOptions:
    commit: str
    now: datetime
    prune: bool = False
    revert: bool = False  # put git back: take back fields changed by hand, remove fields added by hand
    wait: bool = True
    include_break_glass: bool = False
    software: bool = True


def _apply_platform(site: Site, options: DeployOptions, out: TextIO) -> bool:
    base = f"{ROOT}/k8s/{site.cluster}/_platform"
    steps: list[tuple[str, str]] = []
    if f"{base}/namespaces.yaml" in site.tree:
        steps.append(("platform namespaces", site.tree[f"{base}/namespaces.yaml"]))
    charts = releases(site.fleet.software, site.cluster) if options.software else []
    if charts:
        version = site.kubectl.server_version()
        for release in charts:
            out.write(f"rendering {release.name} ({release.spec.chart} {release.version})\n")
            steps.append((release.name, helm_template(site.tools.helm, template_args(release, version), values(release, site.fleet.platform))))
    steps += [("platform objects", site.tree[f"{base}/platform.yaml"]), ("admission policies", site.tree[f"{base}/admission.yaml"])]
    for label, manifests in steps:
        result = site.kubectl.apply(manifests, force_conflicts=options.revert)
        if result.code != 0:
            out.write(f"{label}: apply failed\n{result.stderr.strip()}\n")
            return False
        out.write(f"{label}: applied\n")
    return True


def _hand_changes(kubectl: Kubectl, namespace: str, live: Iterable[ObjectRef], out: TextIO, *, revert: bool) -> None:
    """Fields a person added survive an apply, because nobody else owns them. Report them, or remove them."""
    for ref in live:
        changes = drift.hand_changes(ref)
        if not changes:
            continue
        patch = drift.removal_patch(ref) if revert else None
        if patch is not None:
            kubectl.patch(ref, patch)
            out.writelines(f"{namespace}: removed {ref.kind}/{ref.name} {change}\n" for change in changes)
        elif revert:
            out.writelines(f"{namespace}: cannot remove automatically, edit by hand: {ref.kind}/{ref.name} {change}\n" for change in changes)
        else:
            out.writelines(f"{namespace}: kept a change made by hand: {ref.kind}/{ref.name} {change}; deploy with --revert to remove it\n" for change in changes)


def _deploy_one(site: Site, target: Target, options: DeployOptions, managed_types: list[str], out: TextIO) -> bool:
    result = site.kubectl.apply(site.manifests(target), force_conflicts=options.revert)
    if result.code != 0:
        conflicts = [line.strip() for line in result.stderr.splitlines() if "conflict with" in line]
        if conflicts:
            out.write(f"{target.namespace}: not applied: someone changed fields by hand that git sets differently:\n")
            out.writelines(f"    {line}\n" for line in conflicts)
            out.write("  adopt the change in the spec, or deploy with --revert to put git back\n")
        else:
            out.write(f"{target.namespace}: apply failed\n{result.stderr.strip()}\n")
        return False
    out.write(f"{target.namespace}: applied\n")
    rendered = site.rendered(target)
    live = site.kubectl.objects(managed_types, namespace=target.namespace, selector=_SELECTOR)
    _hand_changes(site.kubectl, target.namespace, live, out, revert=options.revert)
    for ref in drift.prune_plan(rendered, live):
        if options.prune:
            site.kubectl.delete(ref)
            out.write(f"{target.namespace}: deleted {ref.kind}/{ref.name} (no longer in git)\n")
        else:
            out.write(f"{target.namespace}: {ref.kind}/{ref.name} is no longer in git; deploy with --prune to delete it\n")
    ready = True
    if options.wait:
        for name in (drift.object_name(doc) for doc in rendered if doc.get("kind") == "Deployment"):
            status = site.kubectl.rollout_status(target.namespace, name, ROLLOUT_SECONDS)
            if status.code != 0:
                out.write(f"{target.namespace}: {name} did not become ready: {status.stderr.strip() or status.stdout.strip()}\n")
                ready = False
    site.kubectl.annotate_namespace(target.namespace, {drift.DEPLOYED_COMMIT: options.commit, drift.DEPLOYED_AT: options.now.isoformat()}, manager=STAMP_MANAGER)
    return ready


def deploy(site: Site, selection: Selection, options: DeployOptions, out: TextIO) -> int:
    """Apply the selection. One app failing does not stop the others; the exit code says whether any failed."""
    if selection.platform and not _apply_platform(site, options, out):
        out.write("platform failed; apps were not deployed\n")
        return 1
    managed_types = site.types(drift.MANAGED_TYPES)
    live_namespaces = site.namespaces()
    failed: list[str] = []
    for target in selection.targets:
        glass = drift.break_glass_of(live_namespaces.get(target.namespace))
        if glass is not None and glass.active(options.now) and not options.include_break_glass:
            out.write(f"{target.namespace}: skipped, break-glass by {glass.by} until {glass.until:%Y-%m-%d %H:%M %Z}\n")
        elif not _deploy_one(site, target, options, managed_types, out):
            failed.append(target.namespace)
    wanted = {target.namespace for target in targets(site.fleet, site.cluster)}
    out.writelines(f"{name}: on the cluster but no longer in git; its workloads keep running until someone removes them\n" for name in sorted(set(live_namespaces) - wanted))
    if failed:
        out.write(f"failed: {', '.join(sorted(failed))}\n")
    return 1 if failed else 0


def _check_one(site: Site, target: Target, namespace: ObjectRef, managed_types: list[str], all_types: list[str]) -> drift.NamespaceDrift:
    try:
        diff = site.kubectl.diff(site.manifests(target))
        if diff.code > 1:
            return drift.NamespaceDrift(target.namespace, target.app, target.env, error=diff.stderr.strip())
        live = site.kubectl.objects(all_types, namespace=target.namespace)
    except KubeError as error:
        return drift.NamespaceDrift(target.namespace, target.app, target.env, error=str(error))
    managed_kinds = {_KINDS[name] for name in managed_types if name in _KINDS}
    ours = [ref for ref in live if ref.kind in managed_kinds and ref.labels.get("app.kubernetes.io/managed-by") == "berth"]
    stale = drift.prune_plan(site.rendered(target), ours)
    return drift.NamespaceDrift(
        target.namespace,
        target.app,
        target.env,
        changed=tuple(drift.changed_objects(diff.stdout, target.namespace) + [f"{ref.kind}/{ref.name} (removed from git)" for ref in stale]),
        by_hand=tuple(f"{ref.kind}/{ref.name}: {change}" for ref in ours for change in drift.hand_changes(ref)),
        diff=diff.stdout,
        unmanaged=tuple(f"{ref.kind}/{ref.name}" for ref in drift.unmanaged(live)),
        break_glass=drift.break_glass_of(namespace),
        deployed_commit=namespace.annotations.get(drift.DEPLOYED_COMMIT),
    )


def check(site: Site, *, now: datetime) -> drift.Report:
    managed_types = site.types(drift.MANAGED_TYPES)
    all_types = site.types(drift.MANAGED_TYPES + drift.EXTRA_TYPES)
    live_namespaces = site.namespaces()
    chosen = targets(site.fleet, site.cluster)
    results: list[drift.NamespaceDrift] = []
    for target in chosen:
        namespace = live_namespaces.get(target.namespace)
        if namespace is None:
            results.append(drift.NamespaceDrift(target.namespace, target.app, target.env, error="namespace does not exist yet; deploy it"))
        else:
            results.append(_check_one(site, target, namespace, managed_types, all_types))
    wanted = {target.namespace for target in chosen}
    results += [
        drift.NamespaceDrift(name, ref.labels.get(LABEL_APP), ref.labels.get(LABEL_ENV), not_in_git=True, break_glass=drift.break_glass_of(ref))
        for name, ref in sorted(live_namespaces.items())
        if name not in wanted
    ]
    return drift.Report(cluster=site.cluster, checked_at=now, namespaces=tuple(results))


@dataclass(frozen=True)
class Mark:
    namespace: str
    reason: str
    hours: float


def break_glass(kubectl: Kubectl, mark: Mark, *, now: datetime, out: TextIO) -> int:
    if not any(ref.name == mark.namespace for ref in kubectl.namespaces(_SELECTOR)):
        out.write(f"error: {mark.namespace} is not a berth namespace on this cluster\n")
        return 1
    glass = drift.BreakGlass(by=kubectl.whoami(), reason=mark.reason, until=now + timedelta(hours=mark.hours))
    kubectl.annotate_namespace(mark.namespace, {drift.BREAK_GLASS: glass.encode()}, manager=drift.BREAK_GLASS_MANAGER)
    out.write(f"{mark.namespace}: break-glass by {glass.by} until {glass.until:%Y-%m-%d %H:%M %Z}. Deploys skip it until then.\n")
    out.write("Change what you need with kubectl. Then put it in the spec, or run `berth breakglass --end` and deploy with --revert to undo it.\n")
    return 0


def end_break_glass(kubectl: Kubectl, namespace: str, out: TextIO) -> int:
    kubectl.annotate_namespace(namespace, {drift.BREAK_GLASS: None}, manager=drift.BREAK_GLASS_MANAGER)
    out.write(f"{namespace}: break-glass ended; the next deploy applies git again\n")
    return 0
