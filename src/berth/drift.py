"""Where a cluster differs from git, and how to say so. Pure: `berth.cli` gathers the facts with `berth.kube`.

Three kinds of difference are reported for each app namespace:
- changed: an object berth renders differs from git, found with a server-side diff;
- unmanaged: an object nobody rendered, such as a ConfigMap made by hand during an incident;
- not in git: a berth namespace on the cluster whose app or environment file has been removed.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from berth.kube import FIELD_MANAGER, STAMP_MANAGER, ObjectRef
from berth.render.k8s import GROUP, KEEP, MANAGED

BREAK_GLASS = f"{GROUP}/break-glass"
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
EXTRA_TYPES = ("secrets", "pods", "jobs.batch", "statefulsets.apps", "daemonsets.apps", "ingresses.networking.k8s.io", "roles.rbac.authorization.k8s.io")
# Created by Kubernetes itself in every namespace.
_BUILT_IN = frozenset({("ServiceAccount", "default"), ("ConfigMap", "kube-root-ca.crt")})
_NEVER_PRUNED = frozenset({"Namespace", "PersistentVolumeClaim"})
_KEEP_KEY, _KEEP_VALUE = next(iter(KEEP.items()))
_MANAGED_KEY, _MANAGED_VALUE = next(iter(MANAGED.items()))


@dataclass(frozen=True)
class BreakGlass:
    """A namespace someone changed by hand on purpose. Deploys skip it until `until`, and the report says who."""

    by: str
    reason: str
    until: datetime

    def encode(self) -> str:
        return json.dumps({"by": self.by, "reason": self.reason, "until": self.until.isoformat()}, sort_keys=True)

    @staticmethod
    def decode(text: str) -> "BreakGlass | None":
        try:
            decoded: object = json.loads(text)
            if not isinstance(decoded, dict):
                return None
            data = {str(key): value for key, value in decoded.items()}
            until = datetime.fromisoformat(str(data["until"]))
        except (ValueError, KeyError, TypeError):
            return None
        if until.tzinfo is None:
            return None
        return BreakGlass(by=str(data.get("by", "?")), reason=str(data.get("reason", "")), until=until)

    def active(self, now: datetime) -> bool:
        return now < self.until


def break_glass_of(namespace: ObjectRef | None) -> BreakGlass | None:
    if namespace is None or BREAK_GLASS not in namespace.annotations:
        return None
    return BreakGlass.decode(namespace.annotations[BREAK_GLASS])


def prune_plan(rendered: Iterable[Mapping[str, object]], live: Iterable[ObjectRef]) -> list[ObjectRef]:
    """Live objects berth made earlier that the specs no longer render. Never namespaces, volumes, or anything kept."""
    wanted = {(str(doc.get("kind")), object_name(doc)) for doc in rendered}
    return sorted(
        (
            ref
            for ref in live
            if ref.labels.get(_MANAGED_KEY) == _MANAGED_VALUE
            and not ref.owned
            and ref.kind not in _NEVER_PRUNED
            and ref.annotations.get(_KEEP_KEY) != _KEEP_VALUE
            and (ref.kind, ref.name) not in wanted
        ),
        key=lambda ref: (ref.kind, ref.name),
    )


def unmanaged(live: Iterable[ObjectRef]) -> list[ObjectRef]:
    """Objects in an app namespace that nobody rendered and no controller owns."""

    def made_by_hand(ref: ObjectRef) -> bool:
        built_in = (ref.kind, ref.name) in _BUILT_IN or "kubernetes.io/service-account.name" in ref.annotations
        return ref.labels.get(_MANAGED_KEY) != _MANAGED_VALUE and not ref.owned and not built_in

    return sorted(filter(made_by_hand, live), key=lambda ref: (ref.kind, ref.name))


def object_name(doc: Mapping[str, object]) -> str:
    meta = doc.get("metadata")
    return str(meta.get("name")) if isinstance(meta, dict) else ""


_DIFF_HEADER = re.compile(r"^diff -u -N \S+/(?P<object>\S+) \S+$", re.MULTILINE)


def changed_objects(diff: str, namespace: str) -> list[str]:
    """`Kind/name` for each object in `kubectl diff` output. Its file names are `<group>.<version>.<Kind>.<ns>.<name>`."""
    found: list[str] = []
    for match in _DIFF_HEADER.finditer(diff):
        parts = match["object"].split(".")
        kind_at = next((index for index, part in enumerate(parts) if part[:1].isupper()), None)
        if kind_at is None:
            continue
        rest = parts[kind_at + 1 :]
        if rest[:1] == [namespace] and len(rest) > 1:
            rest = rest[1:]
        found.append(f"{parts[kind_at]}/{'.'.join(rest)}")
    return found


@dataclass(frozen=True)
class NamespaceDrift:
    namespace: str
    app: str | None
    env: str | None
    changed: tuple[str, ...] = ()
    by_hand: tuple[str, ...] = ()  # "Kind/name: manager: path" for fields a person set
    diff: str = ""
    unmanaged: tuple[str, ...] = ()
    break_glass: BreakGlass | None = None
    deployed_commit: str | None = None
    not_in_git: bool = False
    error: str | None = None

    @property
    def in_sync(self) -> bool:
        return not (self.changed or self.by_hand or self.unmanaged or self.not_in_git or self.error is not None)


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


def hand_changes(ref: ObjectRef) -> list[str]:
    """`manager: path` for every field a person set, rather than berth or a Kubernetes controller."""
    found: list[str] = []
    for entry in ref.managed_fields:
        if entry.subresource == "status" or entry.manager in EXPECTED_MANAGERS:
            continue
        found += [f"{entry.manager}: {path}" for path in field_paths(entry.fields) if path not in _HARMLESS and not path.startswith(".metadata.managedFields")]
    return found


def removal_patch(ref: ObjectRef) -> dict[str, object] | None:
    """A strategic merge patch deleting every field a person added, or None if the kind cannot take one."""
    if ref.kind not in _PATCHABLE:
        return None
    patch: dict[str, object] = {}
    for entry in ref.managed_fields:
        if entry.subresource == "status" or entry.manager in EXPECTED_MANAGERS:
            continue
        _merge(patch, _removal(entry.fields))
    return patch or None


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
    if item.changed:
        parts.append("differs from git: " + ", ".join(item.changed))
    if item.by_hand:
        parts.append("changed by hand: " + ", ".join(item.by_hand))
    if item.unmanaged:
        parts.append("made by hand: " + ", ".join(item.unmanaged))
    if item.break_glass is not None:
        state = "until" if item.break_glass.active(now) else "EXPIRED"
        parts.append(f"break-glass by {item.break_glass.by} {state} {item.break_glass.until:%Y-%m-%d %H:%M %Z} ({item.break_glass.reason})")
    elif item.changed or item.by_hand or item.unmanaged:
        parts.append("not marked")
    return "; ".join(parts)


def text(report: Report) -> str:
    attention = report.attention
    lines = [f"berth drift on {report.cluster}, {report.checked_at:%Y-%m-%d %H:%M %Z}: {len(report.namespaces) - len(attention)} in sync, {len(attention)} need attention"]
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
