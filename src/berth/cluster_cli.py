"""The commands that talk to a cluster: deploy, drift and breakglass. `berth.cli` parses the rest and dispatches."""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO, assert_never

from berth import drift, gitio, notify, operate
from berth.fleet import Fleet
from berth.kube import Kubectl, Tools

COMMANDS = frozenset({"deploy", "drift", "breakglass"})

type Checked = Callable[[Path, TextIO], tuple[Fleet, dict[str, str]] | None]
"""Load the worktree only if `berth check` passes, writing the problems otherwise."""


@dataclass(frozen=True)
class Connection:
    """Which cluster to talk to. kubectl's context is named after the cluster unless --context says otherwise."""

    cluster: str
    context: str | None
    kubeconfig: str | None

    def kubectl(self, tools: Tools) -> Kubectl:
        return Kubectl(tools.kubectl, self.context or self.cluster, self.kubeconfig)


@dataclass(frozen=True)
class Deploy:
    root: Path
    connection: Connection
    app: str | None
    env: str | None
    changed_from: str | None
    prune: bool
    revert: bool
    wait: bool
    include_break_glass: bool
    software: bool


@dataclass(frozen=True)
class Drift:
    root: Path
    connections: tuple[Connection, ...]
    slack: bool
    details: Path | None


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


type Command = Deploy | Drift | BreakGlassStart | BreakGlassEnd


def _connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context", help="kubectl context (default: the cluster name)")
    parser.add_argument("--kubeconfig")


def add_commands(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    deploy = sub.add_parser("deploy", help="apply rendered/ to a cluster with kubectl server-side apply")
    deploy.add_argument("--cluster", required=True)
    _connection_args(deploy)
    deploy.add_argument("--app", help="only this app")
    deploy.add_argument("--env", help="with --app: only this environment")
    deploy.add_argument("--changed-from", metavar="REF", help="only what changed since this git ref (the platform too, if it changed)")
    deploy.add_argument("--prune", action="store_true", help="delete objects berth made that git no longer has (never namespaces or volumes)")
    deploy.add_argument("--revert", action="store_true", help="put git back: take back fields changed by hand and remove fields added by hand")
    deploy.add_argument("--no-wait", action="store_true", help="do not wait for rollouts")
    deploy.add_argument("--include-break-glass", action="store_true", help="also deploy namespaces marked break-glass")
    deploy.add_argument("--no-software", action="store_true", help="leave platform software (Helm charts) alone")
    check = sub.add_parser("drift", help="report where clusters differ from git")
    check.add_argument("--cluster", action="append", required=True, help="repeatable")
    _connection_args(check)
    check.add_argument("--slack", action="store_true", help="post the summary to SLACK_WEBHOOK_URL")
    check.add_argument("--details", type=Path, help="write the full diffs here")
    glass = sub.add_parser("breakglass", help="mark an app environment as changed by hand on purpose, or end that")
    glass.add_argument("app")
    glass.add_argument("--env", required=True)
    glass.add_argument("--reason", help="required to start")
    glass.add_argument("--hours", type=float, default=8.0, help="how long deploys skip it (default 8)")
    glass.add_argument("--end", action="store_true", help="remove the mark")
    _connection_args(glass)


def parse(ns: argparse.Namespace, root: Path, error: Callable[[str], object]) -> Command:
    match ns.command:
        case "deploy":
            connection = Connection(ns.cluster, ns.context, ns.kubeconfig)
            return Deploy(root, connection, ns.app, ns.env, ns.changed_from, ns.prune, ns.revert, not ns.no_wait, ns.include_break_glass, not ns.no_software)
        case "drift":
            if ns.context is not None and len(ns.cluster) > 1:
                error("--context works with one --cluster; name contexts after clusters to check several")
            return Drift(root, tuple(Connection(cluster, ns.context, ns.kubeconfig) for cluster in ns.cluster), ns.slack, ns.details)
        case _:
            where = AppEnv(ns.app, ns.env, ns.context, ns.kubeconfig)
            if ns.end:
                return BreakGlassEnd(root, where)
            if not ns.reason:
                error("breakglass needs --reason (or --end)")
            return BreakGlassStart(root, where, ns.reason, ns.hours)


def _deploy(cmd: Deploy, checked: Checked, out: TextIO, now: datetime) -> int:
    loaded = checked(cmd.root, out)
    if loaded is None:
        return 1
    fleet, tree = loaded
    cluster = cmd.connection.cluster
    if cluster not in fleet.platform.clusters:
        out.write(f"error: unknown cluster {cluster!r} ({', '.join(fleet.platform.clusters)})\n")
        return 1
    changed = None if cmd.changed_from is None else gitio.changed_paths(gitio.at_ref(cmd.root, cmd.changed_from), tree)
    selection = operate.select(operate.targets(fleet, cluster), cluster, app=cmd.app, env=cmd.env, changed=changed)
    if not selection.targets and not selection.platform:
        out.write("nothing to deploy\n")
        return 0
    tools = Tools()
    site = operate.Site(fleet, tree, cmd.connection.kubectl(tools), tools, cluster)
    options = operate.DeployOptions(commit=gitio.head_commit(cmd.root), now=now, prune=cmd.prune, revert=cmd.revert, wait=cmd.wait, include_break_glass=cmd.include_break_glass, software=cmd.software)
    return operate.deploy(site, selection, options, out)


def _drift(cmd: Drift, checked: Checked, out: TextIO, now: datetime) -> int:
    loaded = checked(cmd.root, out)
    if loaded is None:
        return 1
    fleet, tree = loaded
    tools = Tools()
    reports = [operate.check(operate.Site(fleet, tree, c.kubectl(tools), tools, c.cluster), now=now) for c in cmd.connections]
    out.writelines(drift.text(report) for report in reports)
    if cmd.details is not None:
        cmd.details.write_text("\n".join(drift.details(report) for report in reports))
    if cmd.slack:
        notify.post(notify.SlackSettings(), drift.slack(reports))  # pydantic-settings reads SLACK_WEBHOOK_URL
    return 0


def _kubectl(fleet: Fleet, where: AppEnv, out: TextIO) -> Kubectl | None:
    if where.env not in fleet.platform.environments:
        out.write(f"error: unknown environment {where.env!r}\n")
        return None
    return Kubectl(Tools().kubectl, where.context or fleet.platform.environments[where.env].cluster, where.kubeconfig)


def run(cmd: Command, platform_of: Callable[[Path, TextIO], Fleet | None], checked: Checked, out: TextIO, now: datetime) -> int:
    match cmd:
        case Deploy():
            return _deploy(cmd, checked, out, now)
        case Drift():
            return _drift(cmd, checked, out, now)
        case BreakGlassStart() | BreakGlassEnd():
            fleet = platform_of(cmd.root, out)
            kubectl = None if fleet is None else _kubectl(fleet, cmd.where, out)
            if kubectl is None:
                return 1
            namespace = f"{cmd.where.app}-{cmd.where.env}"
            if isinstance(cmd, BreakGlassEnd):
                return operate.end_break_glass(kubectl, namespace, out)
            return operate.break_glass(kubectl, operate.Mark(namespace, cmd.reason, cmd.hours), now=now, out=out)
        case _:
            assert_never(cmd)
