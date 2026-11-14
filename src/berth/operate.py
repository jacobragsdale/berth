"""Deploy, check drift, and break glass against one cluster.

A deploy brings each app namespace to what git renders, independently of every other:

- Each namespace records a digest of the manifests it last applied in full, rollouts included. A deploy applies the
  namespaces whose digest differs, so one that failed is retried by the next run, and one that did not change costs
  nothing. No git tag or "changed since" bookkeeping is needed.
- Environments go in promotion order. An app environment is held while the environment it promotes from failed in
  this run, on this cluster or an earlier one (`hold_from`), so a bad change to one app stops at its first environment
  and no other app waits for it.
- Namespaces deploy in parallel, and one failing never stops another. The exit code says whether any did.

The decisions come from `berth.drift` and the pure functions here; the rest gathers facts with `berth.kube` and
reports in plain lines, so a pipeline log reads as a record of what happened.
"""

import hashlib
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, TextIO, TypeIs

from pydantic import BaseModel, ConfigDict, Field

from berth import drift, yamlio
from berth.drift import BreakGlass
from berth.fleet import Fleet, Instance
from berth.kube import STAMP_MANAGER, ApiServer, Applied, KubeError, ObjectRef, Tools, helm_template, ref
from berth.render import context, manifests_path
from berth.render.pod import GROUP, LABEL_APP, LABEL_ENV, MANAGED
from berth.render.software import Release, releases, template_args, values

SELECTOR = ",".join(f"{key}={value}" for key, value in MANAGED.items())
NOT_BERTH = ",".join(f"{key}!={value}" for key, value in MANAGED.items())
ROLLOUT_SECONDS = 600
JOBS = 8
_SHORT = 12
_SHOWN = 10  # platform changes named in an approval reason


class State(StrEnum):
    DEPLOYED = "deployed"  # applied in full, and every rollout finished
    UNCHANGED = "unchanged"  # already runs what git renders
    FAILED = "failed"
    HELD = "held"  # not tried: the environment it promotes from failed in this run
    SKIPPED = "skipped"  # not tried, on purpose: break-glass, or a newer commit is already deployed there


SETTLED = frozenset({State.DEPLOYED, State.UNCHANGED})  # the namespace runs what git renders
_STOPPED = frozenset({State.FAILED, State.HELD})  # what holds the next environment


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Outcome(_Model):
    app: str
    env: str
    state: State
    detail: str = ""
    break_glass: bool = False  # skipped because someone broke glass there, so it may not run what git renders


class Plan(_Model):
    """What a deploy would do on one cluster, decided without changing it. A `deployed` outcome means "would deploy",
    assuming every namespace before it deploys. `approval` says why a person should look before the deploy runs."""

    cluster: str
    commit: str
    platform: list[str] = Field(default_factory=list)  # what the platform step would change
    namespaces: dict[str, Outcome] = Field(default_factory=dict)
    deletions: dict[str, list[str]] = Field(default_factory=dict)  # Kind/name by namespace
    brake: list[str] = Field(default_factory=list)  # the prune brake, if this run would stop at it
    approval: list[str] = Field(default_factory=list)


class Results(_Model):
    """What one deploy did on one cluster. The next cluster's deploy reads it to hold apps, and `berth publish` reads it
    to publish only what now runs."""

    cluster: str
    commit: str
    platform: Literal["deployed", "failed", "skipped"]  # skipped: a deploy of one app leaves the platform alone
    namespaces: dict[str, Outcome] = Field(default_factory=dict)

    def by_app_env(self) -> dict[tuple[str, str], Outcome]:
        return {(item.app, item.env): item for item in self.namespaces.values()}

    @property
    def exit_code(self) -> int:
        """1: the platform failed and no app was deployed. 2: some app environments failed or were held. 0: neither."""
        if self.platform == "failed":
            return 1
        return 2 if any(item.state in _STOPPED for item in self.namespaces.values()) else 0


@dataclass(frozen=True)
class Target:
    app: str
    env: str
    namespace: str
    path: str  # its manifests in the rendered tree
    digest: str
    configmaps: frozenset[str] = frozenset()  # the versioned ConfigMaps it renders, by name
    source: str | None = None  # the namespace of the environment it promotes from, when that is on the same cluster
    source_digest: str = ""  # and the digest of what git renders there


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def targets(fleet: Fleet, cluster: str, files: Mapping[str, str]) -> list[Target]:
    """The app namespaces on one cluster, in promotion order (dev, qa, prod), then by app."""
    order = fleet.platform.promotion_order()
    found = {
        (instance.app, instance.env): Target(instance.app, instance.env, instance.namespace, path, digest(files[path]), _configmaps(fleet, instance))
        for instance in fleet.instances()
        if fleet.platform.environments[instance.env].cluster == cluster and (path := manifests_path(cluster, instance.namespace)) in files
    }
    for (app, env), target in found.items():
        source = found.get((app, fleet.platform.environments[env].promotes_from or ""))
        if source is not None:
            found[(app, env)] = replace(target, source=source.namespace, source_digest=source.digest)
    return sorted(found.values(), key=lambda target: (order.index(target.env), target.app))


def _configmaps(fleet: Fleet, instance: Instance) -> frozenset[str]:
    """Named from the specs, so retiring old ones never needs the rendered YAML parsed for every namespace."""
    ctx = context(fleet, instance)
    return frozenset(name for name in (ctx.config_map, ctx.files_map) if name is not None)


def _never(_commit: str) -> bool:
    return False


@dataclass(frozen=True)
class DeployOptions:
    commit: str
    now: datetime
    app: str | None = None  # only this app, and leave the platform alone
    env: str | None = None  # with app: only this environment
    everything: bool = False  # also apply namespaces that already run what git renders
    prune: bool = True  # delete what git dropped from the namespaces it still renders, and stop retired environments
    prune_all: bool = False  # delete it even past the platform's prune limit
    revert: bool = False  # put git back: take back fields changed by hand, remove fields added by hand
    wait: bool = True
    include_break_glass: bool = False
    software: bool = True
    jobs: int = JOBS
    plan: bool = False  # decide and say what a deploy would do, without changing the cluster
    holds: Mapping[tuple[str, str], Outcome] = field(default_factory=dict)  # outcomes from earlier clusters in this run
    newer: Callable[[str], bool] = _never  # whether a commit deployed on the cluster descends from this one

    @property
    def reapply(self) -> bool:
        # Hand changes do not change what git renders, so reverting them means applying even unchanged namespaces.
        return self.everything or self.revert


@dataclass(frozen=True)
class Seen:
    """What one look at a cluster found: berth's app namespaces, break-glass marks, and objects by namespace."""

    namespaces: Mapping[str, ObjectRef]
    marks: Mapping[str, BreakGlass]
    managed: Mapping[str, list[ObjectRef]]  # what berth made
    others: Mapping[str, list[ObjectRef]] = field(default_factory=dict)  # everything else, when drift asks for it


def _breaks_glass(mark: BreakGlass | None, options: DeployOptions) -> TypeIs[BreakGlass]:
    """Whether a deploy must leave a namespace alone because someone broke glass there."""
    return mark is not None and mark.active(options.now) and not options.include_break_glass


def decide(target: Target, seen: Seen, outcomes: Mapping[tuple[str, str], Outcome], fleet: Fleet, options: DeployOptions) -> Outcome | None:
    """What to do with one app namespace, without touching it: an outcome that needs no deploy, or None to deploy it."""

    def outcome(state: State, detail: str = "") -> Outcome:
        return Outcome(app=target.app, env=target.env, state=state, detail=detail)

    namespace, mark = seen.namespaces.get(target.namespace), seen.marks.get(target.namespace)
    annotations = namespace.annotations if namespace is not None else {}
    if annotations.get(drift.DEPLOYED_DIGEST) == target.digest and not options.reapply:
        return outcome(State.UNCHANGED)
    source = fleet.platform.environments[target.env].promotes_from
    before = outcomes.get((target.app, source)) if source is not None else None
    if before is not None and (before.state in _STOPPED or before.break_glass):
        why = f"was skipped for break-glass in {source}" if before.break_glass else f"{before.state} in {source} in this run"
        return outcome(State.HELD, f"{target.app} {why}, and {target.env} promotes from {source}")
    if before is None and target.source is not None:
        # Not part of this run, as with --app X --env prod: what "ran in qa" means is what qa's namespace is stamped with.
        source_ns = seen.namespaces.get(target.source)
        if source_ns is None or source_ns.annotations.get(drift.DEPLOYED_DIGEST) != target.source_digest:
            return outcome(State.HELD, f"{target.source} does not run what git renders for it and is not deployed in this run, and {target.env} promotes from {source}")
    if _breaks_glass(mark, options):
        return Outcome(app=target.app, env=target.env, state=State.SKIPPED, detail=f"break-glass by {mark.by} until {mark.until:%Y-%m-%d %H:%M %Z} ({mark.reason})", break_glass=True)
    deployed = annotations.get(drift.DEPLOYED_COMMIT)
    if deployed is not None and deployed != options.commit and options.newer(deployed):
        return outcome(State.SKIPPED, f"a newer commit ({deployed[:_SHORT]}) is already deployed here")
    return None


def hold_from(results: Iterable[Results]) -> dict[tuple[str, str], Outcome]:
    held: dict[tuple[str, str], Outcome] = {}
    for item in results:
        held |= item.by_app_env()
    return held


@dataclass(frozen=True)
class Site:
    """One cluster, as berth sees it: the specs, what they render, and its API server."""

    fleet: Fleet
    files: Mapping[str, str]
    api: ApiServer
    tools: Tools
    cluster: str

    def docs(self, path: str) -> list[dict[str, object]]:
        text = self.files.get(path)
        return [] if text is None else [{str(k): v for k, v in doc.items()} for doc in yamlio.load_all(text, source=path) if isinstance(doc, dict)]

    def rendered(self, target: Target) -> list[dict[str, object]]:
        return self.docs(target.path)

    @property
    def expected(self) -> frozenset[str]:
        """Field managers whose changes are not a person's: berth's, Kubernetes' controllers, and the platform's own."""
        return drift.EXPECTED_MANAGERS | frozenset(self.fleet.platform.deploys.field_managers)

    def look(self, *, others: bool = False) -> Seen:
        """Everything a deploy or drift check needs to know, in one list call per kind, however many namespaces."""
        namespaces = {item.name: item for item in self.api.list(self.api.resource("v1", "Namespace"), labels=SELECTOR)}
        configmaps = self.api.resource("v1", "ConfigMap")
        found = {item.namespace: drift.break_glass_of(item) for item in self.api.list(configmaps, fields=f"metadata.name={drift.BREAK_GLASS}")}
        marks = {namespace: mark for namespace, mark in found.items() if namespace is not None and mark is not None}
        managed = self.objects(drift.MANAGED_TYPES, SELECTOR)
        rest = self.objects(drift.MANAGED_TYPES + drift.EXTRA_TYPES, NOT_BERTH) if others else {}
        return Seen(namespaces, marks, managed, rest)

    def objects(self, types: Iterable[str], labels: str) -> dict[str, list[ObjectRef]]:
        """Objects of these types across the cluster, by namespace: one list per type, however many namespaces."""
        found: dict[str, list[ObjectRef]] = {}
        for name in types:
            resource = self.api.resource_type(name)
            if resource is None:
                continue  # the cluster does not serve it, e.g. Gateway API before Envoy Gateway is installed
            for item in self.api.list(resource, labels=labels):
                if item.namespace is not None:
                    found.setdefault(item.namespace, []).append(item)
        return found


class _Log:
    """Lines from parallel work, written whole so that one namespace's lines stay together."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._lock = threading.Lock()

    def write(self, lines: Iterable[str]) -> None:
        with self._lock:
            self._out.writelines(f"{line}\n" for line in lines)


# Platform


def _chart_key(release: Release) -> str:
    name = f"chart-{release.name}"
    return f"{GROUP}/{name if len(name) <= 63 else 'chart-' + digest(release.name)[:16]}"  # noqa: PLR2004 - annotation name limit


def _chart_digest(release: Release, site: Site, kube_version: str) -> str:
    spec = release.spec
    return digest(f"{spec.repo} {spec.chart} {release.version} {kube_version} {yamlio.dump(values(release, site.fleet.platform))}")


def _apply_all(site: Site, label: str, docs: list[dict[str, object]], options: DeployOptions, *, default_namespace: str | None = None) -> list[str]:
    problems: list[str] = []
    for doc in docs:
        applied = site.api.apply(doc, force=options.revert, default_namespace=default_namespace)
        problems += [f"{label}: {applied.kind}/{applied.name}: {conflict.field}: {conflict.message}" for conflict in applied.conflicts]
        if applied.error is not None:
            problems.append(f"{label}: {applied.kind}/{applied.name}: {applied.error}")
    return problems


def _apply_platform(site: Site, options: DeployOptions, log: _Log) -> bool:
    base = f"k8s/{site.cluster}/_platform"
    problems = _apply_all(site, "platform namespaces", site.docs(f"{base}/namespaces.yaml"), options)
    charts = releases(site.fleet.software, site.cluster) if options.software and not problems else []
    kube_version = site.api.server_version() if charts else ""
    namespaces = {item.name: item for item in site.api.list(site.api.resource("v1", "Namespace"))} if charts else {}
    for release in charts:
        wanted, namespace = _chart_digest(release, site, kube_version), release.install.namespace
        live = namespaces.get(namespace)
        if live is not None and live.annotations.get(_chart_key(release)) == wanted and not options.reapply:
            log.write([f"{release.name}: unchanged ({release.spec.chart} {release.version})"])
            continue
        log.write([f"{release.name}: rendering {release.spec.chart} {release.version}"])
        rendered = helm_template(site.tools.helm, template_args(release, kube_version), values(release, site.fleet.platform))
        docs = [{str(k): v for k, v in doc.items()} for doc in yamlio.load_all(rendered, source=release.name) if isinstance(doc, dict)]
        chart_problems = _apply_all(site, release.name, docs, options, default_namespace=namespace)
        if not chart_problems:
            # One field manager per release, so that two releases in one namespace each keep their own stamp.
            site.api.annotate(ObjectRef.of("v1", "Namespace", namespace), {_chart_key(release): wanted}, manager=f"{STAMP_MANAGER}-{release.name}")
            log.write([f"{release.name}: applied"])
        problems += chart_problems
    if not problems:
        problems += _apply_all(site, "platform objects", site.docs(f"{base}/platform.yaml"), options)
        problems += _apply_all(site, "admission policies", site.docs(f"{base}/admission.yaml"), options)
    log.write(problems or ["platform: applied"])
    return not problems


# One app namespace


def _hand_changes(site: Site, ns: str, applied: Iterable[Applied], options: DeployOptions) -> list[str]:
    """Fields a person added survive an apply, because nobody else owns them. Report them, or remove them."""
    lines: list[str] = []
    for item in applied:
        if item.stored is None:
            continue
        stored = ref(item.stored)
        changes = drift.hand_changes(stored, site.expected)
        patch = drift.removal_patch(stored, site.expected) if options.revert and changes else None
        if patch is not None:
            site.api.patch(stored, patch)
            lines += [f"{ns}: removed {stored.kind}/{stored.name} {change}" for change in changes]
        elif options.revert:
            lines += [f"{ns}: cannot remove automatically, edit by hand: {stored.kind}/{stored.name} {change}" for change in changes]
        else:
            lines += [f"{ns}: kept a change made by hand: {stored.kind}/{stored.name} {change}; deploy with --revert to remove it" for change in changes]
    return lines


def _prune(site: Site, ns: str, docs: list[dict[str, object]], live: list[ObjectRef], options: DeployOptions) -> list[str]:
    lines: list[str] = []
    for gone in drift.prune_plan(docs, live):
        if options.prune:
            site.api.delete(gone)
            lines.append(f"{ns}: deleted {gone.kind}/{gone.name} (no longer in git)")
        else:
            lines.append(f"{ns}: kept {gone.kind}/{gone.name}, which git no longer renders (--no-prune)")
    return lines


def _not_ready(site: Site, ns: str, applied: list[Applied]) -> list[str]:
    """Wait, within one budget per namespace, for External Secrets to sync each ExternalSecret applied, then for each
    Deployment's rollout; one line for each that did not finish. A jobs-only app has no rollout, so a secret missing
    from Key Vault would otherwise show only when its next scheduled run fails."""
    deadline = time.monotonic() + ROLLOUT_SECONDS
    lines: list[str] = []
    for item in applied:
        if item.kind == "ExternalSecret" and item.stored is not None:
            generation = _mapping_int(item.stored.get("metadata"), "generation")
            why = site.api.secret_synced(ns, item.name, generation, deadline)
            lines += [f"{ns}: ExternalSecret/{item.name} {why}"] if why is not None else []
    for item in applied:
        if item.kind == "Deployment":
            why = site.api.rollout(ns, item.name, max(0.0, deadline - time.monotonic()))
            lines += [f"{ns}: {item.name} did not become ready: {why}"] if why is not None else []
    return lines


def _mapping_int(value: object, key: str) -> int:
    found = value.get(key) if isinstance(value, Mapping) else None
    return found if isinstance(found, int) else 0


_AUTOSCALER = "HorizontalPodAutoscaler"  # rendered with the name of the Deployment it scales


def _hand_over_replicas(site: Site, ns: str, docs: list[dict[str, object]], live: list[ObjectRef]) -> tuple[set[str], list[str]]:
    """Pass each Deployment's replica count between git and its autoscaler before git's version is applied, so that
    neither change of hands moves a pod. Returns the Deployments whose count git takes back, and what was done."""
    deployments = {item.name: item for item in live if item.kind == "Deployment"}
    dropped = {item.name: item for item in drift.prune_plan(docs, live) if item.kind == _AUTOSCALER}
    reclaimed: set[str] = set()
    lines: list[str] = []
    for doc in docs:
        name = drift.object_name(doc)
        current = deployments.get(name) if doc.get("kind") == "Deployment" else None
        count = drift.replicas_to_hold(doc, current)
        if count is not None:
            hold = {"apiVersion": doc.get("apiVersion"), "kind": "Deployment", "metadata": {"name": name, "namespace": ns}, "spec": {"replicas": count}}
            held = site.api.apply(hold, manager=drift.HANDOVER_MANAGER, force=True)
            if not held.ok:
                msg = f"Deployment/{name}: could not hold its replica count for the autoscaler: {held.error}"
                raise KubeError(msg)
            lines.append(f"{ns}: Deployment/{name} keeps {count} replicas until its autoscaler takes over")
        elif drift.reclaims_replicas(doc, current, site.expected):
            reclaimed.add(name)
            if name in dropped:
                # Deleted before the apply, not with the rest of what git dropped after it, and even with --no-prune:
                # in between, the autoscaler would go on overriding the count git now sets.
                site.api.delete(dropped[name])
                lines.append(f"{ns}: deleted {_AUTOSCALER}/{name}: git sets the replica count now")
    return reclaimed, lines


def _apply_namespace(site: Site, target: Target, options: DeployOptions, live: list[ObjectRef]) -> tuple[State, str, list[str]]:
    ns, docs = target.namespace, site.rendered(target)
    reclaimed, handed_over = _hand_over_replicas(site, ns, docs, live)
    applied = [site.api.apply(doc, force=options.revert or (doc.get("kind") == "Deployment" and drift.object_name(doc) in reclaimed)) for doc in docs]
    live = [item for item in live if not (item.kind == _AUTOSCALER and item.name in reclaimed)]  # deleted above, if git dropped them
    conflicts = [(item, conflict) for item in applied for conflict in item.conflicts]
    lines: list[str] = []
    if conflicts:
        lines.append(f"{ns}: not applied: someone changed fields by hand that git sets differently:")
        lines += [f"    {item.kind}/{item.name} {conflict.field}: {conflict.message}" for item, conflict in conflicts]
        lines.append("  adopt the change in the spec, or deploy with --revert to put git back")
    lines += [f"{ns}: {item.kind}/{item.name}: {item.error}" for item in applied if item.error is not None]
    if lines:
        return State.FAILED, "conflicts with changes made by hand" if conflicts else "apply failed", handed_over + lines
    lines = [f"{ns}: applied {len(applied)} objects", *handed_over, *_hand_changes(site, ns, applied, options), *_prune(site, ns, docs, live, options)]
    not_ready = _not_ready(site, ns, applied) if options.wait else []
    if not_ready:
        return State.FAILED, "not ready: " + "; ".join(line.removeprefix(f"{ns}: ") for line in not_ready), lines + not_ready
    stamps = {drift.DEPLOYED_DIGEST: target.digest, drift.DEPLOYED_COMMIT: options.commit, drift.DEPLOYED_AT: options.now.isoformat()}
    site.api.annotate(ObjectRef.of("v1", "Namespace", ns), stamps, manager=STAMP_MANAGER)
    return State.DEPLOYED, "", lines


def _deploy_one(site: Site, target: Target, options: DeployOptions, live: list[ObjectRef]) -> tuple[Outcome, list[str]]:
    try:
        state, detail, lines = _apply_namespace(site, target, options, live)
    except KubeError as error:
        state, detail, lines = State.FAILED, str(error), [f"{target.namespace}: {error}"]
    return Outcome(app=target.app, env=target.env, state=state, detail=detail), lines


def _retire(site: Site, settled: list[Target], live: Mapping[str, list[ObjectRef]], now: datetime, log: _Log) -> None:
    """Retire versioned ConfigMaps in namespaces that run what git renders: note when each stopped being rendered,
    and delete it once the grace period has passed."""
    for target in settled:
        objects = live.get(target.namespace, [])
        plan = drift.superseded(objects, target.configmaps | drift.configmaps_used(objects), now)
        for item in plan.mark:
            site.api.annotate(item, {drift.SUPERSEDED_AT: now.isoformat()}, manager=STAMP_MANAGER)
        for item in plan.unmark:
            site.api.annotate(item, {}, manager=STAMP_MANAGER)
        for item in plan.delete:
            site.api.delete(item)
        log.write(f"{target.namespace}: deleted ConfigMap/{item.name}, superseded more than {drift.SUPERSEDED_GRACE.days} days ago" for item in plan.delete)


def _retired(every: list[Target], seen: Seen, options: DeployOptions) -> list[str]:
    """berth's namespaces on the cluster with no environment file in git, whose workloads a full deploy stops."""
    if options.app is not None:
        return []
    wanted = {target.namespace for target in every}
    return sorted(name for name in seen.namespaces if name not in wanted)


def _would_prune(site: Site, chosen: list[Target], retired: list[str], seen: Seen, options: DeployOptions) -> dict[str, list[ObjectRef]]:
    """What this run would delete, by namespace, before anything is applied: what git dropped from each namespace the
    run may deploy, and what berth made in retired environments. Held namespaces are counted too, to err on the side
    of the brake."""
    planned: dict[str, list[ObjectRef]] = {}
    for target in chosen:
        namespace = seen.namespaces.get(target.namespace)
        current = namespace is not None and namespace.annotations.get(drift.DEPLOYED_DIGEST) == target.digest
        if not (current and not options.reapply) and not _breaks_glass(seen.marks.get(target.namespace), options):
            planned[target.namespace] = drift.prune_plan(site.rendered(target), seen.managed.get(target.namespace, []))
    for name in retired:
        if not _breaks_glass(seen.marks.get(name), options):
            planned[name] = drift.prune_plan([], seen.managed.get(name, []))
    return {name: items for name, items in planned.items() if items}


def _stop_retired(site: Site, retired: list[str], seen: Seen, options: DeployOptions, log: _Log) -> None:
    """Delete what berth made in each retired environment, so its workloads stop. What a person must decide stays:
    the Namespace, its volume claims, and anything marked to keep."""
    for name in retired:
        gone = drift.prune_plan([], seen.managed.get(name, []))
        mark = seen.marks.get(name)
        if _breaks_glass(mark, options):
            log.write([f"{name}: on the cluster but no longer in git; left alone, break-glass by {mark.by} until {mark.until:%Y-%m-%d %H:%M %Z}"])
        elif options.prune:
            for item in gone:
                site.api.delete(item)
            # It no longer runs what it was stamped with, so should git bring it back, the next deploy applies it.
            site.api.annotate(ObjectRef.of("v1", "Namespace", name), {}, manager=STAMP_MANAGER)
            stopped = f"deleted {len(gone)} objects berth made there" if gone else "nothing berth made is left there"
            log.write([f"{name}: on the cluster but no longer in git; {stopped}. Delete the Namespace, with its volume claims, once nobody needs them"])
        else:
            log.write([f"{name}: on the cluster but no longer in git; kept {len(gone)} objects berth made there (--no-prune), so its workloads keep running"])


def _brake(site: Site, seen: Seen, planned: Mapping[str, list[ObjectRef]], options: DeployOptions) -> list[str]:
    if not options.prune or options.prune_all:
        return []
    whole = sum(len(items) for name, items in seen.managed.items() if name in seen.namespaces)
    return drift.prune_brake(planned, whole, site.fleet.platform.deploys.prune_limit)


def _would_change(site: Site, doc: Mapping[str, object]) -> bool:
    """Whether applying a platform object would change it: a server-side dry run of git's version, against the live one."""
    applied = site.api.apply(doc, force=True, dry_run=True)
    if applied.stored is None:
        return True  # it would not apply as it is, which is a change worth a look too
    meta = doc.get("metadata")
    namespace = meta.get("namespace") if isinstance(meta, Mapping) else None
    live = site.api.get(site.api.resource(str(doc.get("apiVersion")), str(doc.get("kind"))), drift.object_name(doc), str(namespace) if namespace is not None else None)
    return bool(drift.object_diff("", live, applied.stored))


def _platform_changes(site: Site, options: DeployOptions) -> list[str]:
    """What the platform step would change: charts whose digest differs, and platform objects a dry run would change."""
    changes: list[str] = []
    charts = releases(site.fleet.software, site.cluster) if options.software else []
    if charts:
        kube_version = site.api.server_version()
        namespaces = {item.name: item for item in site.api.list(site.api.resource("v1", "Namespace"))}
        for release in charts:
            live = namespaces.get(release.install.namespace)
            if options.reapply or live is None or live.annotations.get(_chart_key(release)) != _chart_digest(release, site, kube_version):
                changes.append(f"{release.name} ({release.spec.chart} {release.version})")
    for part in ("namespaces", "platform", "admission"):
        changes += [f"{doc.get('kind')}/{drift.object_name(doc)}" for doc in site.docs(f"k8s/{site.cluster}/_platform/{part}.yaml") if _would_change(site, doc)]
    return changes


def approval(plan: Plan, fleet: Fleet, every: Iterable[Target]) -> list[str]:
    """Why a person should approve a deploy before it runs, or nothing. Only a cluster with a production environment
    asks: when the platform would change there, or more of production's app environments than the limit at once."""
    production = {env for env in fleet.platform.envs_on(plan.cluster) if fleet.platform.environments[env].production}
    if not production:
        return []
    reasons: list[str] = []
    if plan.platform:
        shown = ", ".join(plan.platform[:_SHOWN]) + (f" and {len(plan.platform) - _SHOWN} more" if len(plan.platform) > _SHOWN else "")
        reasons.append(f"the platform would change on {plan.cluster}: {shown}")
    whole = sum(target.env in production for target in every)
    changing = sum(item.env in production and item.state is State.DEPLOYED for item in plan.namespaces.values())
    limit = fleet.platform.deploys.production_limit
    if limit.exceeded(changing, whole):
        reasons.append(f"{changing} of {whole} production app environments would change at once, more than deploys.production_limit allows ({limit.floor}, or {limit.share:.0%})")
    return reasons


def plan(site: Site, options: DeployOptions, out: TextIO) -> Plan:
    """Decide what a deploy would do, and say so, reading the cluster but changing nothing."""
    log = _Log(out)
    every = targets(site.fleet, site.cluster, site.files)
    chosen = [t for t in every if options.app in (None, t.app) and options.env in (None, t.env)]
    seen = site.look()
    retired = _retired(every, seen, options)
    planned = _would_prune(site, chosen, retired, seen, options) if options.prune else {}
    platform = _platform_changes(site, options) if options.app is None else []
    log.write([f"plan for {site.cluster} at {options.commit[:_SHORT]}:"])
    if options.app is None:
        log.write([f"platform: would change {', '.join(platform)}" if platform else "platform: unchanged"])
    outcomes: dict[tuple[str, str], Outcome] = dict(options.holds)
    results: dict[str, Outcome] = {}
    for env in site.fleet.platform.promotion_order():
        for target in (t for t in chosen if t.env == env):
            decided = decide(target, seen, outcomes, site.fleet, options) or Outcome(app=target.app, env=target.env, state=State.DEPLOYED, detail="would deploy")
            results[target.namespace] = outcomes[(target.app, target.env)] = decided
    deletions = {name: [f"{item.kind}/{item.name}" for item in items] for name, items in planned.items() if name in retired or results[name].state is State.DEPLOYED}
    for name, outcome in sorted(results.items()):
        if outcome.state is not State.UNCHANGED:
            log.write([f"{name}: {outcome.detail}" if outcome.state is State.DEPLOYED else f"{name}: {outcome.state}: {outcome.detail}"])
        log.write(f"  would delete {item}" for item in deletions.get(name, []))
    log.write(f"{name}: on the cluster but no longer in git; would delete {len(deletions.get(name, []))} objects berth made there" for name in retired)
    brake = _brake(site, seen, planned, options)
    log.write(brake)
    counts = {state: sum(item.state is state for item in results.values()) for state in State}
    log.write([", ".join(f"{count} {'would deploy' if state is State.DEPLOYED else state}" for state, count in counts.items() if count) or "no app environments on this cluster"])
    made = Plan(cluster=site.cluster, commit=options.commit, platform=platform, namespaces=dict(sorted(results.items())), deletions=deletions, brake=brake)
    made = made.model_copy(update={"approval": approval(made, site.fleet, every)})
    log.write([f"approval needed: {'; '.join(made.approval)}" if made.approval else "approval: not needed"])
    return made


def deploy(site: Site, options: DeployOptions, out: TextIO) -> Results:
    """Apply what differs, environment by environment. One app failing does not stop the others."""
    log = _Log(out)
    every = targets(site.fleet, site.cluster, site.files)
    chosen = [t for t in every if options.app in (None, t.app) and options.env in (None, t.env)]
    seen = site.look()
    retired = _retired(every, seen, options)
    if options.prune and not options.prune_all:
        brake = _brake(site, seen, _would_prune(site, chosen, retired, seen, options), options)
        if brake:
            log.write(brake)
            return Results(cluster=site.cluster, commit=options.commit, platform="failed")
    platform: Literal["deployed", "failed", "skipped"] = "skipped"
    if options.app is None:
        if not _apply_platform(site, options, log):
            log.write(["platform failed; apps were not deployed"])
            return Results(cluster=site.cluster, commit=options.commit, platform="failed")
        platform = "deployed"
    outcomes: dict[tuple[str, str], Outcome] = dict(options.holds)
    results: dict[str, Outcome] = {}
    with ThreadPoolExecutor(max_workers=max(1, options.jobs)) as pool:
        for env in site.fleet.platform.promotion_order():
            futures = {}
            for target in (t for t in chosen if t.env == env):
                decided = decide(target, seen, outcomes, site.fleet, options)
                if decided is None:
                    futures[pool.submit(_deploy_one, site, target, options, seen.managed.get(target.namespace, []))] = target
                    continue
                results[target.namespace] = outcomes[(target.app, target.env)] = decided
                if decided.state is not State.UNCHANGED:
                    log.write([f"{target.namespace}: {decided.state}: {decided.detail}"])
            for future in as_completed(futures):
                target = futures[future]
                outcome, lines = future.result()
                log.write(lines)
                results[target.namespace] = outcomes[(target.app, target.env)] = outcome
    # Not where someone broke glass: an emergency fix may point a workload at old settings on purpose.
    _retire(site, [t for t in chosen if results[t.namespace].state in SETTLED and t.namespace not in seen.marks], seen.managed, options.now, log)
    _stop_retired(site, retired, seen, options, log)
    counts = {state: sum(item.state is state for item in results.values()) for state in State}
    log.write([", ".join(f"{count} {state}" for state, count in counts.items() if count) or "no app environments on this cluster"])
    stopped = sorted(name for name, item in results.items() if item.state in _STOPPED)
    if stopped:
        log.write([f"failed or held: {', '.join(stopped)}"])
    return Results(cluster=site.cluster, commit=options.commit, platform=platform, namespaces=dict(sorted(results.items())))


# Drift


def _short(commit: str | None) -> str:
    return commit[:_SHORT] if commit is not None else "nothing"


def _object_diffs(site: Site, target: Target, namespace: ObjectRef, live: list[ObjectRef]) -> tuple[list[str], str]:
    """Which rendered objects a server-side dry run of git's version would change, and the diff."""
    by_key = {(item.kind, item.name): item for item in live} | {("Namespace", namespace.name): namespace}
    changed: list[str] = []
    chunks: list[str] = []
    for doc in site.rendered(target):
        label = f"{doc.get('kind')}/{drift.object_name(doc)}"
        applied = site.api.apply(doc, force=True, dry_run=True)
        if applied.stored is None:
            changed.append(f"{label} (cannot dry-run: {applied.error})")
            continue
        current = by_key.get((str(doc.get("kind")), drift.object_name(doc)))
        chunk = drift.object_diff(label, current.body if current is not None else None, applied.stored)
        if chunk:
            changed.append(label)
            chunks.append(chunk)
    return changed, "".join(chunks)


def _namespace_drift(site: Site, target: Target, namespace: ObjectRef, seen: Seen) -> drift.NamespaceDrift:
    docs, live = site.rendered(target), seen.managed.get(target.namespace, [])
    stamped, commit = namespace.annotations.get(drift.DEPLOYED_DIGEST), namespace.annotations.get(drift.DEPLOYED_COMMIT)
    not_deployed = None
    if stamped != target.digest:
        at = namespace.annotations.get(drift.DEPLOYED_AT, "never")
        not_deployed = f"not deployed: git renders changes the last full deploy ({_short(commit)} at {at}) did not apply"
    return drift.NamespaceDrift(
        target.namespace,
        target.app,
        target.env,
        not_deployed=not_deployed,
        changed=tuple(f"{item.kind}/{item.name} (removed from git)" for item in drift.prune_plan(docs, live)),
        missing=tuple(drift.missing(docs, live)),
        by_hand=tuple(f"{item.kind}/{item.name}: {change}" for item in live for change in drift.hand_changes(item, site.expected)),
        unmanaged=tuple(f"{item.kind}/{item.name}" for item in drift.unmanaged(seen.others.get(target.namespace, []))),
        break_glass=seen.marks.get(target.namespace),
        deployed_commit=commit,
    )


def check(site: Site, *, now: datetime, full: bool = False, jobs: int = JOBS) -> drift.Report:
    """Where the cluster differs from git. The API server's record of who set each field says what people changed, so
    only namespaces that look different (or all of them, with `full`) get a server-side diff of every object."""
    chosen = targets(site.fleet, site.cluster, site.files)
    seen = site.look(others=True)
    namespaces = seen.namespaces
    items: dict[str, drift.NamespaceDrift] = {}
    for target in chosen:
        namespace = namespaces.get(target.namespace)
        if namespace is None:
            items[target.namespace] = drift.NamespaceDrift(target.namespace, target.app, target.env, error="namespace does not exist yet; deploy it")
            continue
        items[target.namespace] = _namespace_drift(site, target, namespace, seen)
    suspects = [t for t in chosen if t.namespace in namespaces and (full or items[t.namespace].suspect)]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(_object_diffs, site, t, namespaces[t.namespace], seen.managed.get(t.namespace, [])): t for t in suspects}
        for future, target in futures.items():
            item = items[target.namespace]
            try:
                changed, text = future.result()
            except KubeError as error:
                items[target.namespace] = replace(item, error=str(error))
                continue
            items[target.namespace] = replace(item, changed=(*changed, *item.changed), diff=text)
    wanted = {target.namespace for target in chosen}
    gone = [
        drift.NamespaceDrift(name, ref.labels.get(LABEL_APP), ref.labels.get(LABEL_ENV), not_in_git=True, break_glass=seen.marks.get(name))
        for name, ref in sorted(namespaces.items())
        if name not in wanted
    ]
    return drift.Report(cluster=site.cluster, checked_at=now, namespaces=(*items.values(), *gone))


# Break-glass


@dataclass(frozen=True)
class Mark:
    """A request to break glass in one app environment."""

    app: str
    env: str
    reason: str
    hours: float


def break_glass(api: ApiServer, fleet: Fleet, request: Mark, now: datetime, out: TextIO) -> int:
    app, env = request.app, request.env
    namespace = f"{app}-{env}"
    user, groups = api.whoami()
    allowed = set(fleet.platform.environments[env].break_glass_groups)
    if allowed and not groups & allowed and "system:masters" not in groups:
        out.write(
            f"warning: {user} is not in a break-glass group for {env} ({', '.join(sorted(allowed))}). Activate it in Entra PIM, "
            "then sign in again (kubelogin remove-tokens); until then the cluster refuses your changes.\n"
        )
    mark = BreakGlass(by=user, reason=request.reason, until=now + timedelta(hours=request.hours))
    doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": drift.BREAK_GLASS, "namespace": namespace}, "data": mark.data()}
    applied = api.apply(doc, manager=drift.BREAK_GLASS_MANAGER, force=True)
    if not applied.ok:
        out.write(f"error: {namespace}: could not mark break-glass: {applied.error}\n")
        return 1
    out.write(f"{namespace}: break-glass by {mark.by} until {mark.until:%Y-%m-%d %H:%M %Z}. Deploys skip it until then.\n")
    out.write("Change what you need with kubectl. Then put it in the spec, or run `berth breakglass --end` and deploy with --revert to undo it.\n")
    return 0


def end_break_glass(api: ApiServer, namespace: str, out: TextIO) -> int:
    api.delete(ObjectRef.of("v1", "ConfigMap", drift.BREAK_GLASS, namespace))
    out.write(f"{namespace}: break-glass ended; the next deploy applies git again\n")
    return 0
