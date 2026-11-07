"""What a change does, as a list of dotted-path edits per app and environment, rather than as a text diff."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from berth.fleet import Fleet, Instance
from berth.render import ROOT, namespace_of

APP_LEVEL = ("description", "owners", "source", "image")
ADDED = "added"
PRESENT = "present"


@dataclass(frozen=True)
class Change:
    """One edit. `app` is None for files outside apps/; `env` is None for app-wide fields such as owners."""

    app: str | None
    env: str | None
    path: str
    before: object
    after: object


def flatten(tree: object, prefix: str = "") -> dict[str, object]:
    """Mappings become dotted paths; lists stay whole values, because their order is part of what they mean."""
    if isinstance(tree, Mapping):
        flat: dict[str, object] = {}
        for key, value in tree.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if tree is None:
        return {}
    if isinstance(tree, list):
        return {prefix: json.dumps(tree, sort_keys=True)} if any(isinstance(item, Mapping) for item in tree) else {prefix: tuple(tree)}
    return {prefix: tree}


def _instance_tree(instance: Instance) -> dict[str, object]:
    return flatten(instance.model_dump(mode="json", exclude={"app", "env"}))


def app_of(path: str) -> str | None:
    parts = path.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "apps" else None  # noqa: PLR2004 - apps/<name>/<file>


def spec_changes(base: Fleet, head: Fleet, changed_files: Iterable[str]) -> list[Change]:
    """Edits to the apps whose own files changed. Apps changed only by a platform edit are reported by `affected_apps`."""
    files = sorted(set(changed_files))
    touched = sorted({app for path in files if (app := app_of(path)) is not None})
    changes: list[Change] = []
    for app in touched:
        before, after = base.apps.get(app), head.apps.get(app)
        if before is None or after is None:
            changes.append(Change(app, None, "app", None if before is None else PRESENT, None if after is None else ADDED))
            continue
        for field in APP_LEVEL:
            old, new = getattr(before.spec, field), getattr(after.spec, field)
            if old != new:
                changes.append(Change(app, None, field, old, new))
        changes.extend(Change(app, None, f"repo_file.{path.removeprefix(f'apps/{app}/')}", None, path) for path in files if path in before.other_files or path in after.other_files)
        for env in dict.fromkeys([*base.platform.environments, *head.platform.environments]):
            old_instance, new_instance = base.instance(app, env), head.instance(app, env)
            if old_instance is None and new_instance is None:
                continue
            if old_instance is None or new_instance is None:
                changes.append(Change(app, env, "environment", None if old_instance is None else PRESENT, None if new_instance is None else ADDED))
                continue
            old_flat, new_flat = _instance_tree(old_instance), _instance_tree(new_instance)
            changes.extend(Change(app, env, path, old_flat.get(path), new_flat.get(path)) for path in sorted(set(old_flat) | set(new_flat)) if old_flat.get(path) != new_flat.get(path))
    changes.extend(Change(None, None, f"platform_file.{path}", None, path) for path in files if app_of(path) is None and not path.startswith(f"{ROOT}/"))
    return changes


def affected_apps(base: Fleet, head: Fleet, changed_files: Iterable[str]) -> dict[str, set[str]]:
    """Apps whose rendered output changed, by app, with the environments affected. Owners hear about these."""
    namespaces: dict[str, tuple[str, str]] = {}
    for fleet in (base, head):
        for instance in fleet.instances():
            namespaces[instance.namespace] = (instance.app, instance.env)
    affected: dict[str, set[str]] = {}
    for path in changed_files:
        found = namespace_of(path)
        if found is not None and found[1] in namespaces:
            app, env = namespaces[found[1]]
            affected.setdefault(app, set()).add(env)
            continue
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == ROOT and parts[1] in {"airflow", "controlm"}:  # noqa: PLR2004 - rendered/<scheduler>/<env>/<file>
            app = parts[3].split("__", 1)[0].removesuffix(".json")
            affected.setdefault(app, set()).add(parts[2])
    return affected
