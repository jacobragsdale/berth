"""Check, render and review the deploy repo; release and promote versions; deploy and check clusters."""

import argparse
import contextlib
import difflib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, assert_never

from berth import bots_cli, cluster_cli, diff, edit, explain, gitio, notify, report, validate
from berth.ado import Ado, AdoError, AdoSettings, http_transport
from berth.datadog import DatadogError
from berth.fleet import Fleet, LoadError, load
from berth.kube import KubeError
from berth.model import App, EnvFile, Mirrors, Platform, Software
from berth.registry import RegistryError
from berth.render import ROOT, render
from berth.render.k8s import RenderError
from berth.review import Decision, Proposal, Requirement, Votes, classify, decide, requirements

SCHEMAS = {
    "platform/schema/app.schema.json": App,
    "platform/schema/env.schema.json": EnvFile,
    "platform/schema/platform.schema.json": Platform,
    "platform/schema/software.schema.json": Software,
    "platform/schema/mirrors.schema.json": Mirrors,
}


@dataclass(frozen=True)
class Check:
    root: Path


@dataclass(frozen=True)
class Render:
    root: Path
    out: Path


@dataclass(frozen=True)
class RenderedDiff:
    root: Path
    base: str
    out: Path | None


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


@dataclass(frozen=True)
class Explain:
    root: Path
    app: str
    env: str


@dataclass(frozen=True)
class Images:
    root: Path
    base: str | None


type Command = Check | Render | RenderedDiff | Review | Gate | Release | Promote | New | Schema | Explain | Images | cluster_cli.Command | bots_cli.Command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="berth", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(), help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate and render every spec, and fail if the schemas are stale")
    render_cmd = sub.add_parser("render", help="write the rendered tree, to read it (it is never committed)")
    render_cmd.add_argument("--out", type=Path, help=f"directory to write (default: {ROOT}/ in the repository, which git ignores)")
    diff_cmd = sub.add_parser("diff", help="show what the working tree's changes do to the rendered output, against a base ref")
    diff_cmd.add_argument("--base", default="main")
    diff_cmd.add_argument("--out", type=Path, help="write the full diff here and print only the summary")
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
    explain_cmd = sub.add_parser("explain", help="show an app in one environment: merged settings and where each came from, rendered objects, links")
    explain_cmd.add_argument("app")
    explain_cmd.add_argument("--env", required=True)
    images_cmd = sub.add_parser("images", help="list the image references (registry/repository@digest) the specs deploy")
    images_cmd.add_argument("--base", help="only images that are new relative to this git ref")
    cluster_cli.add_commands(sub)
    bots_cli.add_commands(sub)
    return parser


def parse(argv: Sequence[str]) -> Command:  # noqa: PLR0911, PLR0912 - one return per command keeps the match exhaustive
    parser = _parser()
    ns = parser.parse_args(argv)
    root = Path(ns.root)
    delegated: dict[str, Callable[[argparse.Namespace, Path, Callable[[str], object]], Command]] = dict.fromkeys(cluster_cli.COMMANDS, cluster_cli.parse)
    delegated |= dict.fromkeys(bots_cli.COMMANDS, bots_cli.parse)
    if (handler := delegated.get(ns.command)) is not None:
        return handler(ns, root, parser.error)
    match ns.command:
        case "check":
            return Check(root)
        case "render":
            return Render(root, ns.out if ns.out is not None else root / ROOT)
        case "diff":
            return RenderedDiff(root, ns.base, ns.out)
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
        case "explain":
            return Explain(root, ns.app, ns.env)
        case "images":
            return Images(root, ns.base)
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


def rendered(fleet: Fleet) -> tuple[dict[str, str] | None, list[str]]:
    """The rendered tree, or the problems that stop it: cross-file rules, then anything that fails to render.

    This is what the gate checks with main's berth. It does not compare anything committed, because nothing rendered
    is committed: a change to berth's own renderer is judged on its code, not on output main's code cannot produce.
    """
    problems = validate.problems(fleet)
    if problems:
        return None, problems
    try:
        return render(fleet), []
    except RenderError as error:
        return None, str(error).splitlines()


def _check(cmd: Check, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    fleet, tree = loaded
    _, problems = rendered(fleet)
    problems += validate.pipeline_problems(fleet.platform, tree)
    problems += [f"{path}: stale; run `berth schema`" for path, text in _schemas().items() if tree.get(path) != text]
    out.writelines(f"{problem}\n" for problem in problems)
    if not problems:
        out.write(f"ok: {len(fleet.apps)} apps, {sum(len(app.envs) for app in fleet.apps.values())} app environments\n")
    return 1 if problems else 0


_RENDERED_README_START = "# Rendered output"


def _render(cmd: Render, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    files, problems = rendered(loaded[0])
    if files is None:
        out.writelines(f"{problem}\n" for problem in problems)
        return 1
    target = cmd.out
    existing = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} if target.is_dir() else set()
    readme = target / "README.md"
    if existing and not (readme.is_file() and readme.read_text().startswith(_RENDERED_README_START)):
        out.write(f"error: {target} is not empty and is not a rendered tree; choose another --out\n")
        return 1
    changed = sorted(path for path in set(files) | existing if path not in files or path not in existing or (target / path).read_text() != files[path])
    for path in changed:
        if path in files:
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            (target / path).write_text(files[path])
        else:
            (target / path).unlink()
    for directory in sorted(target.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    out.write(f"rendered {len(files)} files into {target}/ ({len(changed)} changed)\n")
    return 0


def _unified(path: str, before: dict[str, str], after: dict[str, str]) -> str:
    old, new = before.get(path, ""), after.get(path, "")
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}" if path in before else "/dev/null", f"b/{path}" if path in after else "/dev/null")
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


def _rendered_diff(cmd: RenderedDiff, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    head, problems = rendered(loaded[0])
    if head is None:
        out.writelines(f"{problem}\n" for problem in problems)
        return 1
    try:
        base = render(load(gitio.at_ref(cmd.root, cmd.base)))
    except (LoadError, RenderError):
        base = {}  # a base that does not render (before berth existed, say): everything is new
    chunks = {path: _unified(path, base, head) for path in gitio.changed_paths(base, head)}
    if cmd.out is None:
        out.write("".join(chunks.values()))
        return 0
    cmd.out.write_text("".join(chunks.values()))
    count = "1 rendered file changes" if len(chunks) == 1 else f"{len(chunks)} rendered files change"
    out.write(f"{count} against {cmd.base}; the full diff is in {cmd.out}\n")
    for path, chunk in chunks.items():
        lines = chunk.splitlines()[2:]
        out.write(f"  +{sum(line.startswith('+') for line in lines)} -{sum(line.startswith('-') for line in lines)}  {path}\n")
    return 0


# A change to berth itself is judged by main's berth, which renders the old way. Its rendered effect comes from the
# pull request's own build instead (`berth diff`, attached to the run).
_CODE_PATHS = ("src/", "scripts/", "pyproject.toml", "uv.lock")
_CODE_NOTE = "this changes berth itself: main's berth rendered the list above; the pull request build attaches what the new code renders"


@dataclass(frozen=True)
class _Reviewed:
    base: Fleet
    reqs: list[Requirement]
    affected: dict[str, set[str]]
    problems: list[str]
    notes: tuple[str, ...] = ()

    def decide(self, proposal: Proposal, now: datetime) -> Decision:
        if self.problems:
            return Decision("failed", ("berth check fails: fix the specs", *self.problems[:20]))
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
        if change.path == ("version",) and change.app is not None and change.env is not None and change.env in platform.environments
        if (source := platform.environments[change.env].promotes_from) is not None
    }
    history = gitio.version_history(root, base_ref, sources)
    findings = [classify(change, platform, policy, history) for change in changes]
    head_files, problems = rendered(head)
    affected: dict[str, set[str]] = {}
    if head_files is not None:
        with contextlib.suppress(RenderError):  # main renders on main; if it ever does not, there is nothing to compare
            affected = diff.affected_apps(base, head, gitio.changed_paths(render(base), head_files))
    notes = (_CODE_NOTE,) if any(path.startswith(_CODE_PATHS) for path in changed) else ()
    return _Reviewed(base, requirements(findings, base, head), affected, problems, notes)


def _review(cmd: Review, out: TextIO, now: datetime) -> int:
    reviewed = _reviewed(cmd.root, cmd.base, out)
    if reviewed is None:
        return 1
    decision = reviewed.decide(Proposal(cmd.author, Votes(cmd.approved, cmd.rejected), cmd.labels), now)
    out.write(report.text(reviewed.reqs, reviewed.affected, decision, reviewed.notes))
    if cmd.markdown is not None:
        cmd.markdown.write_text(report.markdown(reviewed.reqs, reviewed.affected, decision, reviewed.notes))
    return 0


def _gate(cmd: Gate, out: TextIO, now: datetime) -> int:
    settings = AdoSettings()
    ado = Ado(settings, http_transport(settings.token))
    pr = ado.pull_request(cmd.pr)
    reviewed = _reviewed(cmd.root, cmd.base, out)
    if reviewed is None:
        ado.post_status(cmd.pr, Decision("failed", ("the specs do not load; see the pipeline log",)), cmd.target_url)
        return 1
    decision = reviewed.decide(pr.proposal, now)
    ado.post_status(cmd.pr, decision, cmd.target_url)
    out.write(report.text(reviewed.reqs, reviewed.affected, decision, reviewed.notes))
    if cmd.summary is not None:
        cmd.summary.write_text(report.markdown(reviewed.reqs, reviewed.affected, decision, reviewed.notes))
    return 0


def _images(fleet: Fleet) -> set[str]:
    """Every image the fleet runs, as registry/repository@digest: the digest is what runs, so it is what to check."""
    return {f"{fleet.platform.registry}/{fleet.apps[i.app].spec.repository}@{i.version.split('@', 1)[1]}\t{i.tag}" for i in fleet.instances()}


def _list_images(cmd: Images, out: TextIO) -> int:
    loaded = _load_worktree(cmd.root, out)
    if loaded is None:
        return 1
    images = _images(loaded[0])
    if cmd.base is not None:
        # A base that does not load (before berth existed, say) leaves every image counted as new.
        with contextlib.suppress(LoadError):
            images -= _images(load(gitio.at_ref(cmd.root, cmd.base)))
    out.writelines(f"{line}\n" for line in sorted(images))
    return 0


def _platform_of(root: Path, out: TextIO) -> Fleet | None:
    loaded = _load_worktree(root, out)
    return None if loaded is None else loaded[0]


def _checked(root: Path, out: TextIO) -> tuple[Fleet, dict[str, str]] | None:
    """The worktree's fleet and rendered tree, only if it validates and renders: nothing invalid reaches a cluster."""
    loaded = _load_worktree(root, out)
    if loaded is None:
        return None
    files, problems = rendered(loaded[0])
    if files is None:
        out.writelines(f"{problem}\n" for problem in problems)
        out.write("refusing to touch the cluster until `berth check` passes\n")
        return None
    return loaded[0], files


def run(cmd: Command, out: TextIO, now: datetime) -> int:  # noqa: PLR0911, PLR0912, C901 - one branch per command, checked by assert_never
    try:
        match cmd:
            case Check():
                return _check(cmd, out)
            case Render():
                return _render(cmd, out)
            case RenderedDiff():
                return _rendered_diff(cmd, out)
            case Review():
                return _review(cmd, out, now)
            case Gate():
                return _gate(cmd, out, now)
            case Release():
                path = edit.release(cmd.root, cmd.app, cmd.env, cmd.version)
                out.write(f"{path.relative_to(cmd.root)}: version {cmd.version}\n")
                return 0
            case Promote():
                fleet = _platform_of(cmd.root, out)
                if fleet is None:
                    return 1
                path, version = edit.promote(cmd.root, fleet.platform, cmd.app, cmd.env)
                out.write(f"{path.relative_to(cmd.root)}: version {version}\n")
                return 0
            case New():
                paths = edit.new_app(cmd.root, cmd.app)
                out.writelines(f"created {path.relative_to(cmd.root)}\n" for path in paths)
                return 0
            case Explain():
                fleet = _platform_of(cmd.root, out)
                if fleet is None:
                    return 1
                out.write(explain.text(fleet, cmd.app, cmd.env))
                return 0 if fleet.instance(cmd.app, cmd.env) is not None else 1
            case Images():
                return _list_images(cmd, out)
            case cluster_cli.Deploy() | cluster_cli.Drift() | cluster_cli.BreakGlassStart() | cluster_cli.BreakGlassEnd() | cluster_cli.Publish():
                return cluster_cli.run(cmd, _platform_of, _checked, out, now)
            case bots_cli.RightsizePlan() | bots_cli.RightsizeApply() | bots_cli.RightsizeApps() | bots_cli.MirrorPlan() | bots_cli.MirrorRows() | bots_cli.Propose():
                return bots_cli.run(cmd, _platform_of, out, now)
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
    except (edit.EditError, gitio.GitError, KubeError, notify.NotifyError, DatadogError, RegistryError, AdoError) as error:
        out.write(f"error: {error}\n")
        return 1


def main() -> None:
    sys.exit(run(parse(sys.argv[1:]), sys.stdout, datetime.now(UTC)))
