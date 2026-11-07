"""Where a cluster differs from git, and how to say so. Pure: `berth.operate` gathers the facts with `berth.kube`.

For every app namespace the report says:
- not deployed: git renders something the last successful deploy did not apply;
- changed: an object differs from git, found with a server-side dry run of git's version;
- missing: git renders an object the cluster does not have;
- by hand: fields a person set, read from the API server's record of who set each field;
- unmanaged: objects nobody rendered, such as a ConfigMap made during an incident;
- not in git: a berth namespace on the cluster whose app or environment file has been removed.
"""

import difflib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from berth import yamlio
from berth.kube import FIELD_MANAGER, STAMP_MANAGER, ObjectRef
from berth.render.k8s import KEEP, VERSIONED
from berth.render.pod import GROUP, MANAGED

BREAK_GLASS = "berth-break-glass"  # the ConfigMap in an app namespace that marks it as changed by hand on purpose
BREAK_GLASS_MANAGER = "berth-break-glass"
# Field managers whose changes are expected: berth itself, and Kubernetes' controllers (an autoscaler setting replicas,
# a Deployment's revision annotation). Anyone else, such as kubectl-edit, kubectl-set or kubectl-scale, is a person.
EXPECTED_MANAGERS = frozenset({FIELD_MANAGER, STAMP_MANAGER, BREAK_GLASS_MANAGER, "kube-controller-manager"})
# A restart from kubectl only stamps the pod template; it changes nothing git describes.
_HARMLESS = frozenset({".spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"})
# Kinds a strategic merge patch can edit. Custom resources only take JSON merge patches, which cannot remove list items.
_PATCHABLE = frozenset({"ConfigMap", "ServiceAccount", "PersistentVolumeClaim", "Service", "Deployment", "CronJob", "HorizontalPodAutoscaler", "PodDisruptionBudget", "NetworkPolicy", "RoleBinding"})
DEPLOYED_COMMIT = f"{GROUP}/deployed-commit"
DEPLOYED_AT = f"{GROUP}/deployed-at"
DEPLOYED_DIGEST = f"{GROUP}/deployed-digest"  # a hash of the manifests last applied in full, rollouts included
SUPERSEDED_AT = f"{GROUP}/superseded-at"  # when a versioned ConfigMap stopped being rendered
# A versioned ConfigMap outlives its last render by this long, so a workflow run that started on the old settings,
# or a pod a scheduler starts from an old DAG version, still finds them.
SUPERSEDED_GRACE = timedelta(days=7)

# What berth renders into an app namespace, as kubectl resource types. Prune considers only these.
MANAGED_TYPES = (
    "configmaps",
    "serviceaccounts",
    "persistentvolumeclaims",
    "services",
    "deployments.apps",
    "cronjobs.batch",
    "horizontalpodautoscalers.autoscaling",
    "poddisruptionbudgets.policy",
    "networkpolicies.networking.k8s.io",
    "rolebindings.rbac.authorization.k8s.io",
    "externalsecrets.external-secrets.io",
    "httproutes.gateway.networking.k8s.io",
)
# Also looked at for hand-made objects: things people create in an emergency.
# Not Secrets: External Secrets owns the ones apps use, and the report has no reason to read the rest.
EXTRA_TYPES = ("pods", "jobs.batch", "statefulsets.apps", "daemonsets.apps", "ingresses.networking.k8s.io", "roles.rbac.authorization.k8s.io")
# Created by Kubernetes itself in every namespace, or by berth's own commands.
_BUILT_IN = frozenset({("ServiceAccount", "default"), ("ConfigMap", "kube-root-ca.crt"), ("ConfigMap", BREAK_GLASS)})
_NEVER_PRUNED = frozenset({"Namespace", "PersistentVolumeClaim"})
_KEEP_KEY, _KEEP_VALUE = next(iter(KEEP.items()))
_MANAGED_KEY, _MANAGED_VALUE = next(iter(MANAGED.items()))
_VERSIONED_KEY, _VERSIONED_VALUE = next(iter(VERSIONED.items()))
# Fields the API server sets on every object, which never mean the object differs from git.
_SERVER_METADATA = ("managedFields", "resourceVersion", "generation", "uid", "creationTimestamp", "selfLink")


@dataclass(frozen=True)
class BreakGlass:
    """A namespace someone changed by hand on purpose. Deploys skip it until `until`, and the report says who."""

    by: str
    reason: str
    until: datetime

    def data(self) -> dict[str, str]:
        return {"by": self.by, "reason": self.reason, "until": self.until.isoformat()}

    @staticmethod
    def from_data(data: Mapping[str, object]) -> "BreakGlass | None":
        try:
            until = datetime.fromisoformat(str(data["until"]))
        except (ValueError, KeyError, TypeError):
            return None
        if until.tzinfo is None:
            return None
        return BreakGlass(by=str(data.get("by", "?")), reason=str(data.get("reason", "")), until=until)

    def active(self, now: datetime) -> bool:
        return now < self.until


def break_glass_of(mark: ObjectRef | None) -> BreakGlass | None:
    """The mark a `berth-break-glass` ConfigMap carries, or None if there is none or it is unreadable."""
    if mark is None:
        return None
    data = mark.body.get("data")
    return BreakGlass.from_data({str(key): value for key, value in data.items()}) if isinstance(data, Mapping) else None


def _versioned(ref: ObjectRef) -> bool:
    return ref.kind == "ConfigMap" and ref.labels.get(_VERSIONED_KEY) == _VERSIONED_VALUE


def prune_plan(rendered: Iterable[Mapping[str, object]], live: Iterable[ObjectRef]) -> list[ObjectRef]:
    """Live objects berth made earlier that the specs no longer render. Never namespaces, volumes, anything kept, or
    versioned ConfigMaps, which `superseded` retires on its own schedule."""
    wanted = {(str(doc.get("kind")), object_name(doc)) for doc in rendered}
    return sorted(
        (
            ref
            for ref in live
            if ref.labels.get(_MANAGED_KEY) == _MANAGED_VALUE
            and not ref.owned
            and not _versioned(ref)
            and ref.kind not in _NEVER_PRUNED
            and ref.annotations.get(_KEEP_KEY) != _KEEP_VALUE
            and (ref.kind, ref.name) not in wanted
        ),
        key=lambda ref: (ref.kind, ref.name),
    )


@dataclass(frozen=True)
class Retirement:
    """What to do with an app namespace's versioned ConfigMaps once the namespace runs what git renders."""

    mark: tuple[ObjectRef, ...] = ()  # no longer rendered: note when, so the grace period can start
    unmark: tuple[ObjectRef, ...] = ()  # rendered again (a revert brings old settings back): forget the note
    delete: tuple[ObjectRef, ...] = ()  # unrendered for longer than the grace period


def superseded(live: Iterable[ObjectRef], wanted: Iterable[str], now: datetime, grace: timedelta = SUPERSEDED_GRACE) -> Retirement:
    """Settings live in ConfigMaps named by a hash of their content, so every change leaves the old one behind.

    `wanted` are the names git renders and the names live workloads use; those are never retired.
    """
    wanted = set(wanted)
    mark: list[ObjectRef] = []
    unmark: list[ObjectRef] = []
    delete: list[ObjectRef] = []
    for ref in sorted((ref for ref in live if _versioned(ref)), key=lambda ref: ref.name):
        stamp = ref.annotations.get(SUPERSEDED_AT)
        if ref.name in wanted:
            if stamp is not None:
                unmark.append(ref)
            continue
        try:
            since = datetime.fromisoformat(stamp) if stamp is not None else None
        except ValueError:
            since = None
        if since is None or since.tzinfo is None:
            mark.append(ref)
        elif now - since >= grace:
            delete.append(ref)
    return Retirement(tuple(mark), tuple(unmark), tuple(delete))


def configmaps_used(objects: Iterable[ObjectRef]) -> set[str]:
    """ConfigMaps the live workloads name, whatever git says, so one still in use is never retired."""
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = child.get("name") if isinstance(child, Mapping) else None
                if key in {"configMapRef", "configMap", "configMapKeyRef"} and isinstance(name, str):
                    found.add(name)
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for item in objects:
        if item.kind in {"Deployment", "CronJob"}:
            walk(item.body.get("spec"))
    return found


def unmanaged(live: Iterable[ObjectRef]) -> list[ObjectRef]:
    """Objects in an app namespace that nobody rendered and no controller owns."""

    def made_by_hand(ref: ObjectRef) -> bool:
        built_in = (ref.kind, ref.name) in _BUILT_IN or "kubernetes.io/service-account.name" in ref.annotations
        return ref.labels.get(_MANAGED_KEY) != _MANAGED_VALUE and not ref.owned and not built_in

    return sorted(filter(made_by_hand, live), key=lambda ref: (ref.kind, ref.name))


def missing(rendered: Iterable[Mapping[str, object]], live: Iterable[ObjectRef]) -> list[str]:
    """`Kind/name` for each object git renders into the namespace that the cluster does not have."""
    present = {(ref.kind, ref.name) for ref in live}
    return [f"{doc.get('kind')}/{object_name(doc)}" for doc in rendered if doc.get("kind") != "Namespace" and (str(doc.get("kind")), object_name(doc)) not in present]


def object_name(doc: Mapping[str, object]) -> str:
    meta = doc.get("metadata")
    return str(meta.get("name")) if isinstance(meta, dict) else ""


def comparable(obj: Mapping[str, object] | None) -> dict[str, object]:
    """An object without what the API server keeps for itself (status, versions, the managed-fields record)."""
    if obj is None:
        return {}
    meta = obj.get("metadata")
    kept = {str(key): value for key, value in obj.items() if key != "status"}
    if isinstance(meta, Mapping):
        kept["metadata"] = {str(key): value for key, value in meta.items() if key not in _SERVER_METADATA}
    return kept


def object_diff(label: str, live: Mapping[str, object] | None, wanted: Mapping[str, object] | None) -> str:
    """A unified diff from the live object to what applying git's version would make it; empty when they agree."""
    before, after = comparable(live), comparable(wanted)
    if before == after:
        return ""
    old = yamlio.dump(before).splitlines(keepends=True) if before else []
    new = yamlio.dump(after).splitlines(keepends=True) if after else []
    return "".join(difflib.unified_diff(old, new, f"live/{label}", f"git/{label}"))


@dataclass(frozen=True)
class NamespaceDrift:
    namespace: str
    app: str | None
    env: str | None
    not_deployed: str | None = None  # why the namespace does not yet run what git renders
    changed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    by_hand: tuple[str, ...] = ()  # "Kind/name: manager: path" for fields a person set
    diff: str = ""
    unmanaged: tuple[str, ...] = ()
    break_glass: BreakGlass | None = None
    deployed_commit: str | None = None
    not_in_git: bool = False
    error: str | None = None

    @property
    def in_sync(self) -> bool:
        differs = self.not_deployed is not None or self.changed or self.missing or self.by_hand or self.unmanaged
        return not (differs or self.not_in_git or self.error is not None)

    @property
    def suspect(self) -> bool:
        """Worth a full server-side diff. Otherwise the managed-fields record already shows nobody changed anything."""
        return self.not_deployed is not None or bool(self.missing or self.by_hand or self.changed)


@dataclass(frozen=True)
class Report:
    cluster: str
    checked_at: datetime
    namespaces: tuple[NamespaceDrift, ...]
    links: Mapping[str, str] = field(default_factory=dict)  # e.g. to the pipeline run with the full diffs

    @property
    def attention(self) -> list[NamespaceDrift]:
        return [item for item in self.namespaces if not item.in_sync]


def field_paths(fields: Mapping[str, object], prefix: str = "") -> list[str]:
    """Readable paths for a `fieldsV1` tree. An element owned as a whole (`.`) is one path, not its every field."""
    paths: list[str] = []
    for key, child in fields.items():
        if key == ".":
            continue
        path = prefix + _part(key)
        if isinstance(child, dict) and child and "." not in child:
            paths.extend(field_paths({str(k): v for k, v in child.items()}, path))
        else:
            paths.append(path)
    return paths


def _part(key: str) -> str:
    kind, _, rest = key.partition(":")
    if kind == "f":
        return f".{rest}"
    if kind == "k":
        decoded: object = json.loads(rest)
        items = decoded.items() if isinstance(decoded, dict) else []
        return "[" + ",".join(f"{name}={value}" for name, value in items) + "]"
    if kind == "v":
        return f"[={json.loads(rest)}]"
    return f"[{rest}]"


def _owned(fields: Mapping[str, object], prefix: str = "") -> set[str]:
    """Every path a fieldsV1 tree owns: each leaf field, and each list item it owns as an item (`.`).

    Owning an item is not owning its fields: berth owns the container `main` and the fields it sets in it, and an
    environment variable someone else added to `main` is still theirs.
    """
    paths: set[str] = set()
    for key, child in fields.items():
        if key == ".":
            continue
        path = prefix + _part(key)
        tree = {str(k): v for k, v in child.items()} if isinstance(child, dict) else {}
        if not tree or "." in tree:
            paths.add(path)
        paths |= _owned(tree, path)
    return paths


def _berths(ref: ObjectRef) -> set[str]:
    """The paths berth itself applied. A field berth also owns has been adopted: git now says the same thing."""
    return {path for entry in ref.managed_fields if entry.manager == FIELD_MANAGER for path in _owned(entry.fields)}


def _people(ref: ObjectRef) -> list[tuple[str, Mapping[str, object]]]:
    return [(entry.manager, entry.fields) for entry in ref.managed_fields if entry.subresource != "status" and entry.manager not in EXPECTED_MANAGERS]


def hand_changes(ref: ObjectRef) -> list[str]:
    """`manager: path` for every field a person set that git does not also set."""
    ours = _berths(ref)
    return [f"{manager}: {path}" for manager, fields in _people(ref) for path in field_paths(fields) if path not in _HARMLESS and not path.startswith(".metadata.managedFields") and path not in ours]


def removal_patch(ref: ObjectRef) -> dict[str, object] | None:
    """A strategic merge patch deleting every field a person added that git does not set; None if none can be built."""
    if ref.kind not in _PATCHABLE:
        return None
    ours = _berths(ref)
    patch: dict[str, object] = {}
    for _, fields in _people(ref):
        _merge(patch, _removal(_without(fields, ours)))
    return patch or None


def _without(fields: Mapping[str, object], ours: set[str], prefix: str = "") -> dict[str, object]:
    """The part of a fieldsV1 tree that berth does not also own."""
    kept: dict[str, object] = {}
    for key, child in fields.items():
        if key == ".":
            kept[key] = child
            continue
        path = prefix + _part(key)
        tree = {str(k): v for k, v in child.items()} if isinstance(child, dict) else {}
        if tree and "." not in tree:
            # Only a path through the tree, such as the container `main`: what matters is beneath it.
            inner = _without(tree, ours, path)
            if inner:
                kept[key] = inner
        elif path not in ours:
            kept[key] = child
    return kept


def _removal(fields: Mapping[str, object]) -> dict[str, object]:
    """Turn a fieldsV1 tree into a patch that deletes it: fields become null, list items `$patch: delete`."""
    patch: dict[str, object] = {}
    for key, child in fields.items():
        kind, _, name = key.partition(":")
        if kind != "f":
            continue
        tree = {str(k): v for k, v in child.items()} if isinstance(child, dict) else {}
        if not tree or "." in tree:
            patch[name] = None
        elif any(k.startswith("k:") for k in tree):
            items: list[object] = []
            for item_key, item_tree in tree.items():
                if not item_key.startswith("k:"):
                    continue
                keys: object = json.loads(item_key[2:])
                inner = {str(k): v for k, v in item_tree.items()} if isinstance(item_tree, dict) else {}
                base = dict(keys) if isinstance(keys, dict) else {}
                items.append(base | {"$patch": "delete"} if "." in inner else base | _removal(inner))
            patch[name] = items
        elif any(k.startswith("v:") for k in tree):
            patch[f"$deleteFromPrimitiveList/{name}"] = [json.loads(k[2:]) for k in tree if k.startswith("v:")]
        else:
            patch[name] = _removal(tree)
    return patch


def _merge(into: dict[str, object], patch: Mapping[str, object]) -> None:
    """Combine removal patches from several managers: mappings merge, lists of items to delete concatenate."""
    for key, value in patch.items():
        current = into.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged = {str(k): v for k, v in current.items()}
            _merge(merged, {str(k): v for k, v in value.items()})
            into[key] = merged
        elif isinstance(current, list) and isinstance(value, list):
            into[key] = [*current, *value]
        else:
            into[key] = value


def _describe(item: NamespaceDrift, now: datetime) -> str:
    parts: list[str] = []
    if item.error is not None:
        parts.append(f"could not check: {item.error}")
    if item.not_in_git:
        parts.append("on the cluster but no longer in git")
    if item.not_deployed is not None:
        parts.append(item.not_deployed)
    if item.changed:
        parts.append("differs from git: " + ", ".join(item.changed))
    if item.missing:
        parts.append("missing: " + ", ".join(item.missing))
    if item.by_hand:
        parts.append("changed by hand: " + ", ".join(item.by_hand))
    if item.unmanaged:
        parts.append("made by hand: " + ", ".join(item.unmanaged))
    if item.break_glass is not None:
        state = "until" if item.break_glass.active(now) else "EXPIRED"
        parts.append(f"break-glass by {item.break_glass.by} {state} {item.break_glass.until:%Y-%m-%d %H:%M %Z} ({item.break_glass.reason})")
    elif item.changed or item.by_hand or item.unmanaged or item.missing:
        parts.append("not marked")
    return "; ".join(parts)


def text(report: Report) -> str:
    attention = report.attention
    counts = f"{len(report.namespaces) - len(attention)} in sync, {len(attention)} need attention"
    lines = [f"berth drift on {report.cluster}, {report.checked_at:%Y-%m-%d %H:%M %Z}: {counts}"]
    lines += [f"  {item.namespace}: {_describe(item, report.checked_at)}" for item in attention]
    lines += [f"  {name}: {url}" for name, url in report.links.items()]
    return "\n".join(lines) + "\n"


def slack(reports: Sequence[Report]) -> dict[str, object]:
    """One Slack message for all clusters. Sent every day, even when everything is in sync, so silence means broken."""
    return {"text": "\n".join(text(report).rstrip() for report in reports)}


def details(report: Report) -> str:
    """The full diffs, for the pipeline log."""
    chunks = [text(report)]
    chunks += [f"--- {item.namespace}\n{item.diff}" for item in report.attention if item.diff]
    return "\n".join(chunks)
