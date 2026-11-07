"""Check, render and review the deploy repo; release and promote versions."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, assert_never

from berth import diff, edit, gitio, report, validate
from berth.ado import Ado, AdoSettings, http_transport
from berth.fleet import Fleet, LoadError, load
from berth.model import App, EnvFile
from berth.render import ROOT, render, stale
from berth.review import Decision, Proposal, Requirement, Votes, classify, decide, requirements

SCHEMAS = {"platform/schema/app.schema.json": App, "platform/schema/env.schema.json": EnvFile}


@dataclass(frozen=True)
class Check:
    root: Path


@dataclass(frozen=True)
class Render:
    root: Path
    check: bool


@dataclass(frozen=True)
class Review:
    root: Path
    base: str
    author: str
    approved: frozenset[str]
    rejected: frozenset[str]
    labels: frozenset[str]
    markdown: Path | None


@dataclass(frozen=True)
class Gate:
    root: Path
    base: str
    pr: int
    target_url: str | None
    summary: Path | None


@dataclass(frozen=True)
class Release:
    root: Path
    app: str
    env: str
    version: str


@dataclass(frozen=True)
class Promote:
    root: Path
    app: str
    env: str


@dataclass(frozen=True)
class New:
    root: Path
    app: edit.NewApp


@dataclass(frozen=True)
class Schema:
    root: Path
    check: bool


type Command = Check | Render | Review | Gate | Release | Promote | New | Schema


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="berth", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(), help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate every spec, and fail if rendered/ or the schemas are stale")
    render_cmd = sub.add_parser("render", help="write rendered/ from the specs")
    render_cmd.add_argument("--check", action="store_true", help="only report stale files")
    review_cmd = sub.add_parser("review", help="show who must approve the working tree's changes against a base ref")
    review_cmd.add_argument("--base", default="main")
    review_cmd.add_argument("--author", required=True, help="the identity opening the pull request")
    review_cmd.add_argument("--approve", action="append", default=[], help="simulate an approval (repeatable)")
    review_cmd.add_argument("--reject", action="append", default=[], help="simulate a rejection (repeatable)")
    review_cmd.add_argument("--label", action="append", default=[], help="simulate a pull request label (repeatable)")
    review_cmd.add_argument("--markdown", type=Path, help="also write the report as Markdown")
    gate_cmd = sub.add_parser("gate", help="evaluate an Azure DevOps pull request and post its berth/review status")
    gate_cmd.add_argument("--pr", type=int, required=True)
    gate_cmd.add_argument("--base", required=True, help="the target branch ref, e.g. origin/main")
    gate_cmd.add_argument("--target-url", help="link shown on the status, e.g. this pipeline run")
    gate_cmd.add_argument("--summary", type=Path, help="write the Markdown report here")
    release_cmd = sub.add_parser("release", help="set an app's version in one environment")
    release_cmd.add_argument("app")
    release_cmd.add_argument("--env", required=True)
    release_cmd.add_argument("--version", required=True, help="tag@sha256:digest")
    promote_cmd = sub.add_parser("promote", help="copy the version from the environment this one promotes from")
    promote_cmd.add_argument("app")
    promote_cmd.add_argument("--to", required=True, dest="env")
    new_cmd = sub.add_parser("new", help="scaffold an app with one service")
    new_cmd.add_argument("name")
    new_cmd.add_argument("--description", required=True)
    new_cmd.add_argument("--owner", action="append", required=True, help="at least two (repeatable)")
    new_cmd.add_argument("--source", required=True, help="URL of the repository that builds the image")
    new_cmd.add_argument("--version", required=True, help="tag@sha256:digest")
    new_cmd.add_argument("--env", default="dev")
    schema_cmd = sub.add_parser("schema", help="write the JSON Schemas editors use to complete and check spec files")
    schema_cmd.add_argument("--check", action="store_true")
    return parser


def parse(argv: Sequence[str]) -> Command:  # noqa: PLR0911 - one return per command keeps the match exhaustive
    ns = _parser().parse_args(argv)
    root = Path(ns.root)
    match ns.command:
        case "check":
            return Check(root)
        case "render":
            return Render(root, check=ns.check)
        case "review":
            return Review(root, ns.base, ns.author.lower(), frozenset(name.lower() for name in ns.approve), frozenset(name.lower() for name in ns.reject), frozenset(ns.label), ns.markdown)
        case "gate":
            return Gate(root, ns.base, ns.pr, ns.target_url, ns.summary)
        case "release":
            return Release(root, ns.app, ns.env, ns.version)
        case "promote":
            return Promote(root, ns.app, ns.env)
        case "new":
            return New(root, edit.NewApp(ns.name, ns.description, tuple(ns.owner), ns.source, ns.version, ns.env))
        case "schema":
            return Schema(root, check=ns.check)
        case other:
            msg = f"unknown command {other!r}"
            raise AssertionError(msg)


def _schemas() -> dict[str, str]:
    return {path: json.dumps(model.model_json_schema(), indent=2) + "\n" for path, model in SCHEMAS.items()}


def _load_worktree(root: Path, out: TextIO) -> tuple[Fleet, dict[str, str]] | None:
    tree = gitio.worktree(root)
    try:
        fleet = load(tree)
    except LoadError as error:
        out.writelines(f"{problem}\n" for problem in error.problems)
        return None
    return fleet, tree


def _problems(fleet: Fleet, tree: dict[str, str]) -> list[str]:
    """Everything `berth check` enforces. The gate re-runs it with trusted code, because rendered/ is what deploys."""
    problems = validate.problems(fleet)
    if not problems:
        problems += [f"{path}: stale; run `berth render`" for path in stale(render(fleet), tree)]
        problems += [f"{path}: stale; run `berth schema`" for path, text in _schemas().items() if tree.get(path) != text]
    return problems


def _check(cmd: Check, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    fleet, tree = loaded
    problems = _problems(fleet, tree)
    out.writelines(f"{problem}\n" for problem in problems)
    if not problems:
        out.write(f"ok: {len(fleet.apps)} apps, {sum(len(app.envs) for app in fleet.apps.values())} app environments\n")
    return 1 if problems else 0


def _render(cmd: Render, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    fleet, tree = loaded
    problems = validate.problems(fleet)
    if problems:
        out.writelines(f"{problem}\n" for problem in problems)
        return 1
    expected = render(fleet)
    changed = stale(expected, tree)
    if cmd.check:
        out.writelines(f"{path}: stale\n" for path in changed)
        return 1 if changed else 0
    for path in changed:
        target = cmd.root / path
        if path in expected:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected[path])
        else:
            target.unlink()
    for directory in sorted((cmd.root / ROOT).rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    out.write(f"rendered {len(expected)} files ({len(changed)} changed)\n")
    return 0


@dataclass(frozen=True)
class _Reviewed:
    base: Fleet
    reqs: list[Requirement]
    affected: dict[str, set[str]]
    problems: list[str]

    def decide(self, proposal: Proposal, now: datetime) -> Decision:
        if self.problems:
            return Decision("failed", ("berth check fails: fix the specs or run `berth render`", *self.problems[:20]))
        return decide(self.reqs, proposal, self.base.platform, now)


def _reviewed(root: Path, base_ref: str, out: TextIO) -> _Reviewed | None:
    base_tree = gitio.at_ref(root, base_ref)
    head_tree = gitio.worktree(root)
    try:
        base, head = load(base_tree), load(head_tree)
    except LoadError as error:
        out.writelines(f"{problem}\n" for problem in error.problems)
        return None
    changed = gitio.changed_paths(base_tree, head_tree)
    changes = diff.spec_changes(base, head, changed)
    # The target branch's policy and approvers judge the change, so a pull request cannot loosen its own review.
    platform, policy = base.platform, base.policy
    sources = {
        (change.app, source)
        for change in changes
        if change.path == "version" and change.app is not None and change.env is not None and change.env in platform.environments
        if (source := platform.environments[change.env].promotes_from) is not None
    }
    history = gitio.version_history(root, base_ref, sources)
    findings = [classify(change, platform, policy, history) for change in changes]
    return _Reviewed(base, requirements(findings, base, head), diff.affected_apps(base, head, changed), _problems(head, head_tree))


def _review(cmd: Review, out: TextIO, now: datetime) -> int:
    reviewed = _reviewed(cmd.root, cmd.base, out)
    if reviewed is None:
        return 1
    decision = reviewed.decide(Proposal(cmd.author, Votes(cmd.approved, cmd.rejected), cmd.labels), now)
    out.write(report.text(reviewed.reqs, reviewed.affected, decision))
    if cmd.markdown is not None:
        cmd.markdown.write_text(report.markdown(reviewed.reqs, reviewed.affected, decision))
    return 0


def _gate(cmd: Gate, out: TextIO, now: datetime) -> int:
    settings = AdoSettings()
    ado = Ado(settings, http_transport(settings.system_accesstoken))
    pr = ado.pull_request(cmd.pr)
    reviewed = _reviewed(cmd.root, cmd.base, out)
    if reviewed is None:
        ado.post_status(cmd.pr, Decision("failed", ("the specs do not load; see the pipeline log",)), cmd.target_url)
        return 1
    decision = reviewed.decide(pr.proposal, now)
    ado.post_status(cmd.pr, decision, cmd.target_url)
    out.write(report.text(reviewed.reqs, reviewed.affected, decision))
    if cmd.summary is not None:
        cmd.summary.write_text(report.markdown(reviewed.reqs, reviewed.affected, decision))
    return 0


def _platform_of(root: Path, out: TextIO) -> Fleet | None:
    loaded = _load_worktree(root, out)
    return None if loaded is None else loaded[0]


def run(cmd: Command, out: TextIO, now: datetime) -> int:  # noqa: PLR0911, PLR0912 - one branch per command, checked by assert_never
    try:
        match cmd:
            case Check():
                return _check(cmd, out)
            case Render():
                return _render(cmd, out)
            case Review():
                return _review(cmd, out, now)
            case Gate():
                return _gate(cmd, out, now)
            case Release():
                path = edit.release(cmd.root, cmd.app, cmd.env, cmd.version)
                out.write(f"{path.relative_to(cmd.root)}: version {cmd.version}; run `berth render`\n")
                return 0
            case Promote():
                fleet = _platform_of(cmd.root, out)
                if fleet is None:
                    return 1
                path, version = edit.promote(cmd.root, fleet.platform, cmd.app, cmd.env)
                out.write(f"{path.relative_to(cmd.root)}: version {version}; run `berth render`\n")
                return 0
            case New():
                paths = edit.new_app(cmd.root, cmd.app)
                out.writelines(f"created {path.relative_to(cmd.root)}\n" for path in paths)
                return 0
            case Schema():
                schemas = _schemas()
                stale_paths = [path for path, text in schemas.items() if not (cmd.root / path).is_file() or (cmd.root / path).read_text() != text]
                if cmd.check:
                    out.writelines(f"{path}: stale\n" for path in stale_paths)
                    return 1 if stale_paths else 0
                for path in stale_paths:
                    (cmd.root / path).parent.mkdir(parents=True, exist_ok=True)
                    (cmd.root / path).write_text(schemas[path])
                out.write(f"wrote {len(stale_paths)} schema files\n")
                return 0
            case _:
                assert_never(cmd)
    except (edit.EditError, gitio.GitError) as error:
        out.write(f"error: {error}\n")
        return 1


def main() -> None:
    sys.exit(run(parse(sys.argv[1:]), sys.stdout, datetime.now(UTC)))
