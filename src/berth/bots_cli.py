"""The bots' commands: `rightsize` reads Datadog and proposes sizes; `mirror` finds newer upstream images; `pr` opens
(or updates) the pull request for what a bot changed.

Each bot writes a plan as JSON and changes nothing else, so the pipeline can open one pull request per app from it:
`berth rightsize --apply PLAN --app APP` edits that app's environment file, and `berth release` does the same
for a mirrored image. The plans are the boundary between the network and the repository.
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from string import Template
from typing import TextIO, assert_never

from pydantic import ValidationError

from berth import ado, edit, gitio, mirror, registry, rightsize
from berth.datadog import Datadog, DatadogSettings, http_transport
from berth.fleet import Fleet

COMMANDS = frozenset({"rightsize", "mirror", "pr"})
_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class RightsizePlan:
    root: Path
    env: str
    app: str | None
    plan_out: Path


@dataclass(frozen=True)
class RightsizeApply:
    root: Path
    plan: Path
    app: str
    markdown: Path | None


@dataclass(frozen=True)
class RightsizeApps:
    root: Path
    plan: Path


@dataclass(frozen=True)
class MirrorPlan:
    root: Path
    plan_out: Path


@dataclass(frozen=True)
class MirrorRows:
    root: Path
    plan: Path


@dataclass(frozen=True)
class Propose:
    root: Path
    branch: str
    title: str
    description: str
    auto_complete: bool
    paths: tuple[str, ...]


type Command = RightsizePlan | RightsizeApply | RightsizeApps | MirrorPlan | MirrorRows | Propose


def add_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sizes = sub.add_parser("rightsize", help="propose sizes from Datadog usage (--env), or apply a proposal to one app (--apply)")
    sizes.add_argument("--env", help="query Datadog for this environment's workloads")
    sizes.add_argument("--app", help="with --env: only this app; with --apply: the app to change")
    sizes.add_argument("--plan-out", type=Path, help="with --env: where to write the plan")
    sizes.add_argument("--apply", type=Path, metavar="PLAN", help="a plan written by --env")
    sizes.add_argument("--markdown", type=Path, help="with --apply: write the pull request description here")
    sizes.add_argument("--apps", type=Path, metavar="PLAN", help="list the apps a plan changes, one per line")
    mirrors = sub.add_parser("mirror", help="find newer upstream tags for platform/mirrors.yaml and the apps to release them to")
    found = mirrors.add_mutually_exclusive_group(required=True)
    found.add_argument("--plan-out", type=Path, help="where to write the plan")
    found.add_argument("--rows", type=Path, metavar="PLAN", help="list a plan's releases, one per line: source, target, tag, env, app (tab-separated)")
    pr = sub.add_parser("pr", help="commit what changed, push it to a branch, and open its pull request (or update the open one)")
    pr.add_argument("--branch", required=True, help="the branch to push, one per proposal so a second run updates the first")
    pr.add_argument("--title", required=True)
    described = pr.add_mutually_exclusive_group()
    described.add_argument("--description", default="")
    described.add_argument("--description-file", type=Path)
    pr.add_argument("--auto-complete", action="store_true", help="merge once the gate and checks pass")
    pr.add_argument("--path", action="append", dest="paths", default=[], help="what to commit (repeatable; default: apps)")


def parse(ns: argparse.Namespace, root: Path, error: Callable[[str], object]) -> Command:
    if ns.command == "pr":
        description = ns.description_file.read_text() if ns.description_file is not None else ns.description
        return Propose(root, ns.branch, ns.title, description, ns.auto_complete, tuple(ns.paths) or ("apps",))
    if ns.command == "mirror":
        return MirrorPlan(root, ns.plan_out) if ns.plan_out is not None else MirrorRows(root, ns.rows)
    if ns.apps is not None:
        return RightsizeApps(root, ns.apps)
    if ns.apply is not None:
        if ns.app is None or ns.env is not None:
            error("rightsize --apply needs --app, and no --env")
        return RightsizeApply(root, ns.apply, ns.app, ns.markdown)
    if ns.env is None or ns.plan_out is None:
        error("rightsize needs --env and --plan-out, or --apply and --app")
    return RightsizePlan(root, ns.env, ns.app, ns.plan_out)


def gather(fleet: Fleet, env: str, datadog: Datadog, *, now: datetime, app: str | None = None) -> rightsize.Plan:
    """Query Datadog for every workload in `env` and recommend a size for each."""
    settings = fleet.platform.rightsizing
    end = int(now.timestamp())
    start = end - settings.days * _SECONDS_PER_DAY
    found: list[rightsize.Recommendation] = []
    for workload in rightsize.workloads(fleet, env):
        if app is not None and workload.app != app:
            continue
        fields = {"service": workload.service, "env": env}
        cpu = datadog.points(Template(settings.cpu_query).substitute(fields), start, end)
        memory = datadog.points(Template(settings.memory_query).substitute(fields), start, end)
        oom = datadog.points(Template(settings.oom_query).substitute(fields), start, end, rollup="sum")
        found.append(rightsize.recommend(workload, rightsize.usage(cpu, memory, oom), fleet.platform.sizes, settings))
    return rightsize.Plan(env=env, days=settings.days, recommendations=tuple(found))


def apply(root: Path, fleet: Fleet, plan: rightsize.Plan, app: str) -> list[Path]:
    """Write one app's proposed sizes into its environment file. The pipeline then opens the pull request."""
    changed = [
        edit.resize(root, fleet.platform, edit.Sized(app, item.env, item.path), item.proposed) for item in plan.recommendations if item.app == app and item.changes and item.proposed is not None
    ]
    return sorted(set(changed))


def _load_plan[M: rightsize.Plan | mirror.Plan](model: type[M], path: Path, out: TextIO) -> M | None:
    try:
        return model.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        out.write(f"error: {path}: {error}\n")
        return None


def _propose(cmd: Propose, out: TextIO) -> int:
    if gitio.commit_paths(cmd.root, cmd.paths, cmd.title) is None:
        out.write(f"nothing changed under {', '.join(cmd.paths)}; no pull request\n")
        return 0
    gitio.push_head(cmd.root, "origin", cmd.branch)
    settings = ado.AdoSettings()
    pr_id, created = ado.Ado(settings, ado.http_transport(settings.token)).propose(cmd.branch, cmd.title, cmd.description, auto_complete=cmd.auto_complete)
    out.write(f"{'opened' if created else 'updated'} pull request {pr_id}: {cmd.title}\n")
    return 0


def _rows(cmd: RightsizeApps | MirrorRows, out: TextIO) -> int:
    if isinstance(cmd, RightsizeApps):
        plan = _load_plan(rightsize.Plan, cmd.plan, out)
        if plan is not None:
            out.writelines(f"{app}\n" for app in plan.apps())
        return 0 if plan is not None else 1
    found = _load_plan(mirror.Plan, cmd.plan, out)
    if found is not None:
        out.writelines(f"{u.source}\t{u.target}\t{u.tag}\t{u.env}\t{app}\n" for u in found.updates for app in u.apps)
    return 0 if found is not None else 1


def _rightsize_plan(cmd: RightsizePlan, fleet: Fleet, out: TextIO, now: datetime) -> int:
    if cmd.env not in fleet.platform.environments:
        out.write(f"error: unknown environment {cmd.env!r}\n")
        return 1
    datadog = Datadog(DatadogSettings(), http_transport())  # pydantic-settings reads DD_API_KEY and DD_APP_KEY
    plan = gather(fleet, cmd.env, datadog, now=now, app=cmd.app)
    cmd.plan_out.write_text(plan.model_dump_json(indent=2) + "\n")
    out.write(rightsize.text(plan))
    out.write(f"apps to change: {' '.join(plan.apps()) or 'none'}\n")
    return 0


def _rightsize_apply(cmd: RightsizeApply, fleet: Fleet, out: TextIO) -> int:
    plan = _load_plan(rightsize.Plan, cmd.plan, out)
    if plan is None:
        return 1
    paths = apply(cmd.root, fleet, plan, cmd.app)
    out.writelines(f"{path.relative_to(cmd.root)}: sizes changed\n" for path in paths)
    if cmd.markdown is not None:
        cmd.markdown.write_text(rightsize.markdown(plan, cmd.app))
    return 0


def _mirror_plan(cmd: MirrorPlan, fleet: Fleet, out: TextIO) -> int:
    get = registry.http_get()
    upstream = {name: registry.tags(item.source, get) for name, item in fleet.mirrors.mirrors.items()}
    plan = mirror.plan(fleet, upstream)
    cmd.plan_out.write_text(plan.model_dump_json(indent=2) + "\n")
    out.writelines(f"{update.source}:{update.tag} -> {update.target}:{update.tag}, release to {update.env}: {' '.join(update.apps)}\n" for update in plan.updates)
    if not plan.updates:
        out.write("every mirrored image is current\n")
    return 0


def run(cmd: Command, platform_of: Callable[[Path, TextIO], Fleet | None], out: TextIO, now: datetime) -> int:
    match cmd:
        case Propose():
            return _propose(cmd, out)
        case RightsizeApps() | MirrorRows():
            return _rows(cmd, out)
        case RightsizePlan() | RightsizeApply() | MirrorPlan():
            fleet = platform_of(cmd.root, out)
            if fleet is None:
                return 1
            if isinstance(cmd, RightsizePlan):
                return _rightsize_plan(cmd, fleet, out, now)
            if isinstance(cmd, RightsizeApply):
                return _rightsize_apply(cmd, fleet, out)
            return _mirror_plan(cmd, fleet, out)
        case _:
            assert_never(cmd)
