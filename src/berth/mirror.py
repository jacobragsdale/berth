"""Decide which upstream images to copy into the registry, and which apps to release them to. Pure: the pipeline
imports the image with `az acr import` and opens the release pull requests."""

import re
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict

from berth.fleet import Fleet

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")  # "1.9.1" and "v1.9.1" both count


def _semver(tag: str, suffix: str) -> tuple[int, int, int] | None:
    """X.Y.Z (optionally vX.Y.Z) exactly, plus the suffix: release candidates and other variants never match."""
    if not tag.endswith(suffix):
        return None
    match = _VERSION.fullmatch(tag.removesuffix(suffix) if suffix else tag)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def newest(tags: Iterable[str], track: str, suffix: str = "") -> str | None:
    """The highest X.Y.Z<suffix> tag whose version starts with `track` ("8.2" or "8")."""
    wanted = tuple(int(part) for part in track.split("."))
    candidates = [(version, tag) for tag in tags if (version := _semver(tag, suffix)) is not None and version[: len(wanted)] == wanted]
    return max(candidates)[1] if candidates else None


class Update(BaseModel):
    """One line of the plan: copy `source:tag` to `target:tag`, then release it to these apps' first environment."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
    mirror: str
    source: str
    target: str
    tag: str
    env: str
    apps: tuple[str, ...]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
    updates: tuple[Update, ...] = ()


def first_environment(fleet: Fleet) -> str:
    """Where new versions land: the environment nothing promotes into, dev here."""
    return next(name for name, env in fleet.platform.environments.items() if env.promotes_from is None)


def plan(fleet: Fleet, upstream: Mapping[str, Iterable[str]]) -> Plan:
    """Given each mirror's upstream tags, the releases to make: apps whose first environment runs an older tag."""
    env = first_environment(fleet)
    updates: list[Update] = []
    for name, mirror in sorted(fleet.mirrors.mirrors.items()):
        tag = newest(upstream.get(name, ()), mirror.track, mirror.suffix)
        if tag is None:
            continue
        latest = _semver(tag, mirror.suffix)
        behind: list[str] = []
        for app in sorted(fleet.apps):
            instance = fleet.instance(app, env)
            if fleet.apps[app].spec.image != mirror.target or instance is None:
                continue
            running = _semver(instance.tag, mirror.suffix)
            if running is None or (latest is not None and running < latest):
                behind.append(app)
        if behind:
            updates.append(Update(mirror=name, source=mirror.source, target=mirror.target, tag=tag, env=env, apps=tuple(behind)))
    return Plan(updates=tuple(updates))
