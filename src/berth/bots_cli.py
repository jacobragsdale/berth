"""The bots' commands: `rightsize` reads Datadog and proposes sizes; `mirror` finds newer upstream images.

Each writes a plan as JSON and changes nothing else, so the pipeline can open one pull request per app from it:
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

from berth import edit, mirror, registry, rightsize
from berth.datadog import Datadog, DatadogSettings, http_transport
from berth.fleet import Fleet

COMMANDS = frozenset({"rightsize", "mirror"})
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
class MirrorPlan:
    root: Path
    plan_out: Path


type Command = RightsizePlan | RightsizeApply | MirrorPlan


def add_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sizes = sub.add_parser("rightsize", help="propose sizes from Datadog usage (--env), or apply a proposal to one app (--apply)")
    sizes.add_argument("--env", help="query Datadog for this environment's workloads")
    sizes.add_argument("--app", help="with --env: only this app; with --apply: the app to change")
    sizes.add_argument("--plan-out", type=Path, help="with --env: where to write the plan")
    sizes.add_argument("--apply", type=Path, metavar="PLAN", help="a plan written by --env")
    sizes.add_argument("--markdown", type=Path, help="with --apply: write the pull request description here")
    mirrors = sub.add_parser("mirror", help="find newer upstream tags for platform/mirrors.yaml and the apps to release them to")
    mirrors.add_argument("--plan-out", type=Path, required=True)


def parse(ns: argparse.Namespace, root: Path, error: Callable[[str], object]) -> Command:
    if ns.command == "mirror":
        return MirrorPlan(root, ns.plan_out)
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
    """Write one app's proposed sizes into its environment file. The pipeline then renders and opens the pull request."""
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


def run(cmd: Command, platform_of: Callable[[Path, TextIO], Fleet | None], out: TextIO, now: datetime) -> int:
    fleet = platform_of(cmd.root, out)
    if fleet is None:
        return 1
    match cmd:
        case RightsizePlan():
            if cmd.env not in fleet.platform.environments:
                out.write(f"error: unknown environment {cmd.env!r}\n")
                return 1
            datadog = Datadog(DatadogSettings(), http_transport())  # pydantic-settings reads DD_API_KEY and DD_APP_KEY
            plan = gather(fleet, cmd.env, datadog, now=now, app=cmd.app)
            cmd.plan_out.write_text(plan.model_dump_json(indent=2) + "\n")
            out.write(rightsize.text(plan))
            out.write(f"apps to change: {' '.join(plan.apps()) or 'none'}\n")
            return 0
        case RightsizeApply():
            plan = _load_plan(rightsize.Plan, cmd.plan, out)
            if plan is None:
                return 1
            paths = apply(cmd.root, fleet, plan, cmd.app)
            out.writelines(f"{path.relative_to(cmd.root)}: sizes changed; run `berth render`\n" for path in paths)
            if cmd.markdown is not None:
                cmd.markdown.write_text(rightsize.markdown(plan, cmd.app))
            return 0
        case MirrorPlan():
            get = registry.http_get()
            upstream = {name: registry.tags(item.source, get) for name, item in fleet.mirrors.mirrors.items()}
            plan = mirror.plan(fleet, upstream)
            cmd.plan_out.write_text(plan.model_dump_json(indent=2) + "\n")
            out.writelines(f"{update.source}:{update.tag} -> {update.target}:{update.tag}, release to {update.env}: {' '.join(update.apps)}\n" for update in plan.updates)
            if not plan.updates:
                out.write("every mirrored image is current\n")
            return 0
        case _:
            assert_never(cmd)
