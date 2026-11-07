"""Read repository trees from the worktree or from a git ref, and version history from the log."""

import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from berth.fleet import Tree

_VERSION_LINE = re.compile(r"""^\+version:\s*["']?(?P<version>[^"'\s#]+)""")
_TIMEOUT = 60
# Set inside git hooks and `rebase --exec`; they would point `git -C <root>` at a different repository.
_REPO_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"})


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        env = {key: value for key, value in os.environ.items() if key not in _REPO_OVERRIDES}
        done = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True, check=True, timeout=_TIMEOUT, env=env)  # noqa: S607 - git from PATH, as in CI
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


def changed_paths(base: Tree, head: Tree) -> list[str]:
    return sorted(path for path in set(base) | set(head) if base.get(path) != head.get(path))


def version_history(root: Path, ref: str, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], set[str]]:
    """Every version each apps/<app>/<env>.yaml has had on `ref`, read from the log rather than trusted from a file."""
    history: dict[tuple[str, str], set[str]] = {}
    for app, env in sorted(set(pairs)):
        log = _git(root, "log", "--format=", "-p", "--unified=0", ref, "--", f"apps/{app}/{env}.yaml").decode(errors="replace")
        history[(app, env)] = {match["version"] for line in log.splitlines() if (match := _VERSION_LINE.match(line)) is not None}
    return history
