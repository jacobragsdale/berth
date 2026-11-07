"""Edits people and pipelines make through the CLI rather than by hand: release, promote, new app."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from berth import yamlio
from berth.model import AppName, Identity, Platform, Version


class EditError(ValueError):
    pass


def _env_file(root: Path, app: str, env: str) -> Path:
    path = root / "apps" / app / f"{env}.yaml"
    if not path.is_file():
        msg = f"{path.relative_to(root)} does not exist: {app} is not deployed to {env}"
        raise EditError(msg)
    return path


def _check[T](adapter: TypeAdapter[T], value: object, what: str) -> T:
    try:
        return adapter.validate_python(value)
    except ValidationError as error:
        msg = f"{what}: {error.errors()[0]['msg']}"
        raise EditError(msg) from error


def release(root: Path, app: str, env: str, version: str) -> Path:
    checked = _check(TypeAdapter(Version), version, "version")
    path = _env_file(root, app, env)
    path.write_text(yamlio.set_path(path.read_text(), ("version",), checked, source=str(path)))
    return path


@dataclass(frozen=True)
class Sized:
    """Something with a size in one environment: `path` is ("services", name) or ("workflows", wf, "tasks", task)."""

    app: str
    env: str
    path: tuple[str, ...]


def resize(root: Path, platform: Platform, where: Sized, size: str) -> Path:
    """Set a service's or task's size in one environment's file, keeping its comments."""
    if size not in platform.sizes:
        msg = f"unknown size {size!r} (sizes: {', '.join(platform.sizes)})"
        raise EditError(msg)
    target = _env_file(root, where.app, where.env)
    target.write_text(yamlio.set_path(target.read_text(), (*where.path, "size"), size, source=str(target)))
    return target


def promote(root: Path, platform: Platform, app: str, env: str) -> tuple[Path, str]:
    environment = platform.environments.get(env)
    if environment is None or environment.promotes_from is None:
        msg = f"{env} has no environment to promote from"
        raise EditError(msg)
    source = _env_file(root, app, environment.promotes_from)
    data = yamlio.load(source.read_text(), source=str(source))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str):
        msg = f"{source.relative_to(root)} has no version"
        raise EditError(msg)
    return release(root, app, env, version), version


@dataclass(frozen=True)
class NewApp:
    name: str
    description: str
    owners: tuple[str, ...]
    source: str
    version: str
    env: str


def new_app(root: Path, spec: NewApp) -> list[Path]:
    name, env = spec.name, spec.env
    _check(TypeAdapter(AppName), name, "name")
    for owner in spec.owners:
        _check(TypeAdapter(Identity), owner, "owner")
    _check(TypeAdapter(Version), spec.version, "version")
    directory = root / "apps" / name
    if directory.exists():
        msg = f"apps/{name} already exists"
        raise EditError(msg)
    directory.mkdir(parents=True)
    app_file = directory / "app.yaml"
    app_file.write_text(
        "# yaml-language-server: $schema=../../platform/schema/app.schema.json\n"
        + yamlio.dump(
            {"name": name, "description": spec.description, "owners": list(spec.owners), "source": spec.source, "services": {"api": {"port": 8080, "health": {"path": "/healthz"}, "size": "s"}}}
        )
    )
    env_file = directory / f"{env}.yaml"
    env_file.write_text("# yaml-language-server: $schema=../../platform/schema/env.schema.json\n" + yamlio.dump({"version": spec.version}))
    return [app_file, env_file]
