"""Read repository trees from the worktree or from a git ref, and version history from the log."""

import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from berth.fleet import Tree

_VERSION_LINE = re.compile(r"""^\+version:\s*["']?(?P<version>[^"'\s#]+)""")
_TIMEOUT = 60
# Set inside git hooks and `rebase --exec`; they would point `git -C <root>` at a different repository.
_REPO_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"})


class GitError(RuntimeError):
    pass


def _env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _REPO_OVERRIDES}
    if extra is not None:
        env.update(extra)
    return env


def _git(root: Path, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, check=True, timeout=_TIMEOUT, env=_env(env))  # noqa: S607 - git from PATH, as in CI
    except subprocess.CalledProcessError as error:
        msg = f"git {' '.join(args)} failed: {error.stderr.decode(errors='replace').strip()}"
        raise GitError(msg) from error
    return done.stdout


def worktree(root: Path) -> dict[str, str]:
    """Tracked and untracked (not ignored) files, as they are on disk now."""
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")
    tree: dict[str, str] = {}
    for name in names:
        path = root / name
        if name and path.is_file():
            tree[name] = path.read_bytes().decode(errors="surrogateescape")
    return tree


def at_ref(root: Path, ref: str) -> dict[str, str]:
    listing = _git(root, "ls-tree", "-r", "-z", ref).decode().split("\0")
    entries: list[tuple[str, str]] = []
    for line in listing:
        if not line:
            continue
        meta, name = line.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            entries.append((name, sha))
    if not entries:
        return {}
    output = _git(root, "cat-file", "--batch", stdin="".join(f"{sha}\n" for _, sha in entries).encode())
    tree: dict[str, str] = {}
    offset = 0
    for name, _sha in entries:
        header_end = output.index(b"\n", offset)
        size = int(output[offset:header_end].split()[2])
        start = header_end + 1
        tree[name] = output[start : start + size].decode(errors="surrogateescape")
        offset = start + size + 1  # cat-file ends each blob with a newline
    return tree


def head_commit(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").decode().strip()


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """Whether `descendant` contains `ancestor`. False for a commit this clone does not have."""
    done = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True, check=False, timeout=_TIMEOUT, env=_env())  # noqa: S607 - see _git
    return done.returncode == 0


def remote_branch(root: Path, remote: str, branch: str) -> str | None:
    """Fetch a branch from the remote and return its commit, or None if the remote has no such branch."""
    listed = _git(root, "ls-remote", "--heads", remote, f"refs/heads/{branch}").decode().split()
    if not listed:
        return None
    _git(root, "fetch", "--quiet", "--no-tags", remote, f"refs/heads/{branch}")
    return listed[0]


def tree_of(root: Path, commit: str) -> str:
    return _git(root, "rev-parse", f"{commit}^{{tree}}").decode().strip()


def commit_files(root: Path, files: Mapping[str, str], *, parent: str | None, message: str) -> str:
    """A commit whose tree is exactly `files`, on top of `parent`, made with git's plumbing: neither the worktree nor
    the index is touched, so it works in any checkout. Returns the new commit."""
    with tempfile.TemporaryDirectory() as tmp:
        blobs = Path(tmp) / "blobs"
        names = sorted(files)
        paths = []
        for index, name in enumerate(names):
            path = blobs / str(index)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(files[name].encode(errors="surrogateescape"))
            paths.append(str(path))
        shas = _git(root, "hash-object", "-w", "--stdin-paths", stdin="\n".join(paths).encode()).decode().split() if paths else []
        index_env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        entries = "".join(f"100644 {sha}\t{name}\n" for sha, name in zip(shas, names, strict=True))
        _git(root, "update-index", "--add", "--index-info", stdin=entries.encode(), env=index_env)
        tree = _git(root, "write-tree", env=index_env).decode().strip()
    identity = {"GIT_AUTHOR_NAME": "berth publish", "GIT_AUTHOR_EMAIL": "berth-publish@localhost"}
    identity |= {"GIT_COMMITTER_NAME": identity["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": identity["GIT_AUTHOR_EMAIL"]}
    return _git(root, "commit-tree", tree, *(["-p", parent] if parent is not None else []), "-m", message, env=identity).decode().strip()


def commit_paths(root: Path, paths: Iterable[str], message: str) -> str | None:
    """Commit whatever changed under `paths` and return the commit, or None if nothing did. The author is git's
    usual one, which pipelines set with GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL."""
    _git(root, "add", "--all", "--", *paths)
    staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"], capture_output=True, check=False, timeout=_TIMEOUT, env=_env())  # noqa: S607 - see _git
    if staged.returncode == 0:
        return None
    _git(root, "commit", "--quiet", "--message", message)
    return head_commit(root)


def push_head(root: Path, remote: str, branch: str) -> None:
    """Push HEAD to a branch of its own, replacing it: each bot run rebuilds its branch from main."""
    _git(root, "push", "--quiet", "--force", remote, f"HEAD:refs/heads/{branch}")


def push(root: Path, remote: str, commit: str, branch: str) -> None:
    """Push a commit to a branch. Not forced: if someone else moved the branch since it was read, this fails."""
    _git(root, "push", "--quiet", remote, f"{commit}:refs/heads/{branch}")


def changed_paths(base: Tree, head: Tree) -> list[str]:
    return sorted(path for path in set(base) | set(head) if base.get(path) != head.get(path))


def version_history(root: Path, ref: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], set[str]]:
    """Every version each apps/<app>/<env>.yaml has had on `ref`, read from the log rather than trusted from a file."""
    history: dict[tuple[str, str], set[str]] = {}
    for app, env in sorted(set(pairs)):
        log = _git(root, "log", "--format=", "-p", "--unified=0", ref, "--", f"apps/{app}/{env}.yaml").decode(errors="replace")
        history[(app, env)] = {match["version"] for line in log.splitlines() if (match := _VERSION_LINE.match(line)) is not None}
    return history
