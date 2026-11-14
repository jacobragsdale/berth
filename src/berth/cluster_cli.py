"""The commands that talk to a cluster (deploy, drift, breakglass) and publish what was deployed. `berth.cli` parses
the rest and dispatches."""

import argparse
import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO, assert_never

from pydantic import ValidationError

from berth import drift, gitio, notify, operate, publish
from berth.fleet import Fleet
from berth.kube import ApiServer, Tools

COMMANDS = frozenset({"deploy", "drift", "breakglass", "publish"})

type Checked = Callable[[Path, TextIO], tuple[Fleet, dict[str, str]] | None]
"""Load the worktree and render it, only if `berth check` passes, writing the problems otherwise."""


@dataclass(frozen=True)
class Connection:
    """Which cluster to talk to. kubectl's context is named after the cluster unless --context says otherwise."""

    cluster: str
    context: str | None
    kubeconfig: str | None


@dataclass(frozen=True)
class Deploy:
    root: Path
    connection: Connection
    app: str | None
    env: str | None
    everything: bool
    prune: bool
    prune_all: bool
    revert: bool
    wait: bool
    include_break_glass: bool
    software: bool
    jobs: int
    results_out: Path | None
    hold_from: tuple[Path, ...]
    plan: bool = False
    plan_out: Path | None = None


@dataclass(frozen=True)
class Drift:
    root: Path
    connections: tuple[Connection, ...]
    slack: bool
    details: Path | None
    full: bool
    jobs: int


@dataclass(frozen=True)
class AppEnv:
    """An app environment to break glass in. Its cluster comes from platform.yaml."""

    app: str
    env: str
    context: str | None
    kubeconfig: str | None


@dataclass(frozen=True)
class BreakGlassStart:
    root: Path
    where: AppEnv
    reason: str
    hours: float


@dataclass(frozen=True)
class BreakGlassEnd:
    root: Path
    where: AppEnv


@dataclass(frozen=True)
class Publish:
    root: Path
    cluster: str
    results: Path | None
    remote: str
    branch: str
    push: bool


type Command = Deploy | Drift | BreakGlassStart | BreakGlassEnd | Publish


def _connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context", help="kubectl context (default: the cluster name)")
    parser.add_argument("--kubeconfig")


def add_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    deploy = sub.add_parser("deploy", help="apply what git renders to a cluster, namespace by namespace, where it differs")
    deploy.add_argument("--cluster", required=True)
    _connection_args(deploy)
    deploy.add_argument("--app", help="only this app, and leave the platform alone")
    deploy.add_argument("--env", help="with --app: only this environment")
    deploy.add_argument("--all", action="store_true", dest="everything", help="also apply namespaces that already run what git renders")
    pruning = deploy.add_mutually_exclusive_group()
    pruning.add_argument("--no-prune", action="store_true", help="keep objects berth made that git no longer renders; by default they are deleted (never namespaces or volumes)")
    pruning.add_argument("--prune-all", action="store_true", help="delete them even past platform.yaml's deploys.prune_limit")
    deploy.add_argument("--revert", action="store_true", help="put git back: take back fields changed by hand and remove fields added by hand")
    deploy.add_argument("--no-wait", action="store_true", help="do not wait for rollouts")
    deploy.add_argument("--include-break-glass", action="store_true", help="also deploy namespaces marked break-glass")
    deploy.add_argument("--no-software", action="store_true", help="leave platform software (Helm charts) alone")
    deploy.add_argument("--jobs", type=int, default=operate.JOBS, help=f"namespaces to deploy at once (default {operate.JOBS})")
    deploy.add_argument("--results-out", type=Path, help="write what happened to each namespace here, as JSON")
    deploy.add_argument("--hold-from", type=Path, action="append", default=[], help="results of an earlier cluster's deploy in this run (repeatable)")
    deploy.add_argument("--plan", action="store_true", help="say what the deploy would do, and whether a person should approve it, without changing the cluster")
    deploy.add_argument("--plan-out", type=Path, help="with --plan: write the plan here, as JSON")
    check = sub.add_parser("drift", help="report where clusters differ from git")
    check.add_argument("--cluster", action="append", required=True, help="repeatable")
    _connection_args(check)
    check.add_argument("--slack", action="store_true", help="post the summary to SLACK_WEBHOOK_URL")
    check.add_argument("--details", type=Path, help="write the full diffs here")
    check.add_argument("--full", action="store_true", help="diff every namespace, not only those that look different")
    check.add_argument("--jobs", type=int, default=operate.JOBS, help=f"namespaces to diff at once (default {operate.JOBS})")
    glass = sub.add_parser("breakglass", help="mark an app environment as changed by hand on purpose, or end that")
    glass.add_argument("app")
    glass.add_argument("--env", required=True)
    glass.add_argument("--reason", help="required to start")
    glass.add_argument("--hours", type=float, default=8.0, help="how long deploys skip it (default 8)")
    glass.add_argument("--end", action="store_true", help="remove the mark")
    _connection_args(glass)
    pub = sub.add_parser("publish", help="commit what a deploy applied to one cluster onto the rendered branch, and push it")
    pub.add_argument("--cluster", required=True)
    pub.add_argument("--results", type=Path, help="that deploy's --results-out; without it, the cluster's whole part is replaced")
    pub.add_argument("--remote", default="origin")
    pub.add_argument("--branch", default=publish.BRANCH)
    pub.add_argument("--no-push", action="store_true", help="make the commit and print it, but do not push")


def parse(ns: argparse.Namespace, root: Path, error: Callable[[str], object]) -> Command:
    match ns.command:
        case "deploy":
            if ns.env is not None and ns.app is None:
                error("--env needs --app")
            if ns.plan_out is not None and not ns.plan:
                error("--plan-out needs --plan")
            if ns.plan and ns.results_out is not None:
                error("--results-out records a deploy; with --plan, use --plan-out")
            connection = Connection(ns.cluster, ns.context, ns.kubeconfig)
            return Deploy(
                root,
                connection,
                ns.app,
                ns.env,
                ns.everything,
                not ns.no_prune,
                ns.prune_all,
                ns.revert,
                not ns.no_wait,
                ns.include_break_glass,
                not ns.no_software,
                ns.jobs,
                ns.results_out,
                tuple(ns.hold_from),
                plan=ns.plan,
                plan_out=ns.plan_out,
            )
        case "drift":
            if ns.context is not None and len(ns.cluster) > 1:
                error("--context works with one --cluster; name contexts after clusters to check several")
            return Drift(root, tuple(Connection(cluster, ns.context, ns.kubeconfig) for cluster in ns.cluster), ns.slack, ns.details, ns.full, ns.jobs)
        case "publish":
            return Publish(root, ns.cluster, ns.results, ns.remote, ns.branch, not ns.no_push)
        case _:
            where = AppEnv(ns.app, ns.env, ns.context, ns.kubeconfig)
            if ns.end:
                return BreakGlassEnd(root, where)
            if not ns.reason:
                error("breakglass needs --reason (or --end)")
            return BreakGlassStart(root, where, ns.reason, ns.hours)


def _results(path: Path, out: TextIO) -> operate.Results | None:
    try:
        return operate.Results.model_validate_json(path.read_text())
    except (OSError, ValidationError) as error:
        out.write(f"error: {path}: {error}\n")
        return None


def _deploy(cmd: Deploy, checked: Checked, out: TextIO, now: datetime) -> int:
    loaded = checked(cmd.root, out)
    if loaded is None:
        return 1
    fleet, files = loaded
    cluster = cmd.connection.cluster
    if cluster not in fleet.platform.clusters:
        out.write(f"error: unknown cluster {cluster!r} ({', '.join(fleet.platform.clusters)})\n")
        return 1
    earlier = [_results(path, out) for path in cmd.hold_from]
    if None in earlier:
        return 1
    commit = gitio.head_commit(cmd.root)
    newer = functools.cache(lambda deployed: gitio.is_ancestor(cmd.root, commit, deployed))
    options = operate.DeployOptions(
        commit=commit,
        now=now,
        app=cmd.app,
        env=cmd.env,
        everything=cmd.everything,
        prune=cmd.prune,
        prune_all=cmd.prune_all,
        revert=cmd.revert,
        wait=cmd.wait,
        include_break_glass=cmd.include_break_glass,
        software=cmd.software,
        jobs=cmd.jobs,
        plan=cmd.plan,
        holds=operate.hold_from(item for item in earlier if item is not None),
        newer=newer,
    )
    tools = Tools()
    with ApiServer.connect(tools, cmd.connection.context or cluster, cmd.connection.kubeconfig) as api:
        site = operate.Site(fleet, files, api, tools, cluster)
        if cmd.plan:
            planned = operate.plan(site, options, out)
            if cmd.plan_out is not None:
                cmd.plan_out.write_text(planned.model_dump_json(indent=2) + "\n")
            return 1 if planned.brake else 0
        results = operate.deploy(site, options, out)
    if cmd.results_out is not None:
        cmd.results_out.write_text(results.model_dump_json(indent=2) + "\n")
    return results.exit_code


def _drift(cmd: Drift, checked: Checked, out: TextIO, now: datetime) -> int:
    loaded = checked(cmd.root, out)
    if loaded is None:
        return 1
    fleet, files = loaded
    tools = Tools()
    reports = []
    for connection in cmd.connections:
        with ApiServer.connect(tools, connection.context or connection.cluster, connection.kubeconfig) as api:
            reports.append(operate.check(operate.Site(fleet, files, api, tools, connection.cluster), now=now, full=cmd.full, jobs=cmd.jobs))
    out.writelines(drift.text(report) for report in reports)
    if cmd.details is not None:
        cmd.details.write_text("\n".join(drift.details(report) for report in reports))
    if cmd.slack:
        notify.post(notify.SlackSettings(), drift.slack(reports))  # pydantic-settings reads SLACK_WEBHOOK_URL
    return 0


def _publish(cmd: Publish, checked: Checked, out: TextIO) -> int:
    loaded = checked(cmd.root, out)
    if loaded is None:
        return 1
    fleet, files = loaded
    if cmd.cluster not in fleet.platform.clusters:
        out.write(f"error: unknown cluster {cmd.cluster!r} ({', '.join(fleet.platform.clusters)})\n")
        return 1
    results = None if cmd.results is None else _results(cmd.results, out)
    if cmd.results is not None and results is None:
        return 1
    branch = publish.Branch(cmd.root, cmd.remote, cmd.branch, push=cmd.push)
    made = publish.publish(branch, publish.Deployed(fleet, cmd.cluster, files, results, gitio.head_commit(cmd.root)))
    if made is None:
        out.write(f"{cmd.branch}: {cmd.cluster} already up to date\n")
    else:
        out.write(f"{cmd.branch}: {cmd.cluster} {'published' if cmd.push else 'committed (not pushed)'} as {made}\n")
    return 0


def _break_glass(cmd: BreakGlassStart | BreakGlassEnd, platform_of: Callable[[Path, TextIO], Fleet | None], out: TextIO, now: datetime) -> int:
    fleet = platform_of(cmd.root, out)
    if fleet is None:
        return 1
    environment = fleet.platform.environments.get(cmd.where.env)
    if environment is None:
        out.write(f"error: unknown environment {cmd.where.env!r}\n")
        return 1
    with ApiServer.connect(Tools(), cmd.where.context or environment.cluster, cmd.where.kubeconfig) as api:
        if isinstance(cmd, BreakGlassEnd):
            return operate.end_break_glass(api, f"{cmd.where.app}-{cmd.where.env}", out)
        return operate.break_glass(api, fleet, operate.Mark(cmd.where.app, cmd.where.env, cmd.reason, cmd.hours), now, out)


def run(cmd: Command, platform_of: Callable[[Path, TextIO], Fleet | None], checked: Checked, out: TextIO, now: datetime) -> int:
    match cmd:
        case Deploy():
            return _deploy(cmd, checked, out, now)
        case Drift():
            return _drift(cmd, checked, out, now)
        case Publish():
            return _publish(cmd, checked, out)
        case BreakGlassStart() | BreakGlassEnd():
            return _break_glass(cmd, platform_of, out, now)
        case _:
            assert_never(cmd)
