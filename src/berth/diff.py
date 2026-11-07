"""What a change does, as a list of edits per app and environment, rather than as a text diff."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from berth.fleet import Fleet, Instance
from berth.render import namespace_of

APP_LEVEL = ("description", "owners", "source", "image")
ADDED = "added"
PRESENT = "present"

type FieldPath = tuple[str, ...]
"""A field in one app's merged spec, one key per element, so a key with a dot in it (`indexer.toml`) stays one key."""


def show(path: FieldPath) -> str:
    """A path for people: dotted, with any key that contains a dot in brackets, such as `files[indexer.toml]`."""
    return "".join(f"[{part}]" if "." in part else part if index == 0 else f".{part}" for index, part in enumerate(path))


@dataclass(frozen=True)
class Change:
    """One edit. `app` is None for files outside apps/; `env` is None for app-wide fields such as owners."""

    app: str | None
    env: str | None
    path: FieldPath
    before: object
    after: object


def flatten(tree: object, prefix: FieldPath = ()) -> dict[FieldPath, object]:
    """Mappings become paths; lists stay whole values, because their order is part of what they mean."""
    if isinstance(tree, Mapping):
        flat: dict[FieldPath, object] = {}
        for key, value in tree.items():
            flat.update(flatten(value, (*prefix, str(key))))
        return flat
    if tree is None:
        return {}
    if isinstance(tree, list):
        return {prefix: json.dumps(tree, sort_keys=True)} if any(isinstance(item, Mapping) for item in tree) else {prefix: tuple(tree)}
    return {prefix: tree}


def _instance_tree(instance: Instance) -> dict[FieldPath, object]:
    return flatten(instance.model_dump(mode="json", exclude={"app", "env"}))


def app_of(path: str) -> str | None:
    parts = path.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "apps" else None  # noqa: PLR2004 - apps/<name>/<file>


def _app_changes(base: Fleet, head: Fleet, app: str, files: list[str]) -> list[Change]:
    before, after = base.apps.get(app), head.apps.get(app)
    if before is None or after is None:
        old, new = (None, ADDED) if before is None else (PRESENT, None)
        present = after if after is not None else before
        # A new or retired app also starts or stops in each of its environments, so a first deploy to
        # production is judged as one, whether or not the app is new.
        return [Change(app, None, ("app",), old, new)] + [Change(app, env, ("environment",), old, new) for env in sorted(present.envs if present else ())]
    changes: list[Change] = []
    for field in APP_LEVEL:
        old, new = getattr(before.spec, field), getattr(after.spec, field)
        if old != new:
            changes.append(Change(app, None, (field,), old, new))
    prefix = f"apps/{app}/"
    changes.extend(Change(app, None, ("repo_file", path.removeprefix(prefix)), None, path) for path in files if path in before.other_files or path in after.other_files)
    for env in dict.fromkeys([*base.platform.environments, *head.platform.environments]):
        old_instance, new_instance = base.instance(app, env), head.instance(app, env)
        if old_instance is None and new_instance is None:
            continue
        if old_instance is None or new_instance is None:
            changes.append(Change(app, env, ("environment",), None if old_instance is None else PRESENT, None if new_instance is None else ADDED))
            continue
        old_flat, new_flat = _instance_tree(old_instance), _instance_tree(new_instance)
        changes.extend(Change(app, env, path, old_flat.get(path), new_flat.get(path)) for path in sorted(set(old_flat) | set(new_flat)) if old_flat.get(path) != new_flat.get(path))
    return changes


def _new_callers(base: Fleet, head: Fleet) -> list[Change]:
    """Calling another app changes it too: its network policy starts admitting the caller, so its owners decide."""
    before = base.callers()
    return [Change(callee, env, ("callers", caller), None, ADDED) for (callee, env), callers in sorted(head.callers().items()) for caller in sorted(callers - before.get((callee, env), set()))]


def spec_changes(base: Fleet, head: Fleet, changed_files: Iterable[str]) -> list[Change]:
    """Edits to the apps whose own files changed, and to the apps they start calling. Apps changed only by a platform
    edit are reported by `affected_apps`."""
    files = sorted(set(changed_files))
    changes: list[Change] = []
    for app in sorted({app for path in files if (app := app_of(path)) is not None}):
        changes.extend(_app_changes(base, head, app, files))
    changes.extend(_new_callers(base, head))
    changes.extend(Change(None, None, ("platform_file", path), None, path) for path in files if app_of(path) is None)
    return changes


def affected_apps(base: Fleet, head: Fleet, changed_rendered: Iterable[str]) -> dict[str, set[str]]:
    """Apps whose rendered output changed, by app, with the environments affected. Owners hear about these.

    `changed_rendered` are paths in the rendered tree, as `berth.render.render` names them.
    """
    namespaces: dict[str, tuple[str, str]] = {}
    for fleet in (base, head):
        for instance in fleet.instances():
            namespaces[instance.namespace] = (instance.app, instance.env)
    affected: dict[str, set[str]] = {}
    for path in changed_rendered:
        found = namespace_of(path)
        if found is not None and found[1] in namespaces:
            app, env = namespaces[found[1]]
            affected.setdefault(app, set()).add(env)
            continue
        parts = path.split("/")
        if len(parts) == 3 and parts[0] in {"airflow", "controlm"}:  # noqa: PLR2004 - <scheduler>/<env>/<file>
            app = parts[2].split("__", 1)[0].removesuffix(".json")
            affected.setdefault(app, set()).add(parts[1])
    return affected
