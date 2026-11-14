"""Publish what a deploy applied to the `rendered` branch, which Airflow reads as a Git DAG bundle and the Control-M
stage deploys from.

The branch records what each cluster runs, not merely what main renders. After a cluster's deploy, its part of the
branch is replaced, except for app environments that did not end up running what git renders (failed, held, or
skipped): those keep their previous files. A scheduler then keeps starting pods that its namespace can actually run,
with the ConfigMaps that namespace actually has.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from berth import gitio
from berth.fleet import Fleet
from berth.operate import SETTLED, Results
from berth.render import README, cluster_prefixes, owner_of

BRANCH = "rendered"


def published(fleet: Fleet, cluster: str, files: Mapping[str, str], previous: Mapping[str, str], results: Results | None) -> dict[str, str]:
    """The rendered branch's files after one cluster's deploy. Without results, all of the cluster's part is replaced.

    With results, a file changes only if the deploy covered its namespace and that namespace now runs what git renders.
    A namespace the deploy did not cover (a deploy of one app, say) keeps what the branch had.
    """
    prefixes = cluster_prefixes(fleet.platform, cluster)
    outcomes = results.by_app_env() if results is not None else {}

    def current(path: str) -> bool:
        """Whether `path` should show what main renders now, rather than what the branch had."""
        if results is None:
            return True
        owner = owner_of(path)
        if owner is None:
            return results.platform == "deployed"
        outcome = outcomes.get(owner)
        if outcome is None:
            return results.platform == "deployed"  # a full deploy covered every namespace: this app has gone
        return outcome.state in SETTLED

    tree = {path: text for path, text in previous.items() if not path.startswith(prefixes) or not current(path)}
    tree |= {path: text for path, text in files.items() if path.startswith(prefixes) and current(path)}
    return dict(sorted((tree | {"README.md": README}).items()))


@dataclass(frozen=True)
class Branch:
    root: Path  # a clone of the deploy repository
    remote: str
    name: str = BRANCH
    push: bool = True


@dataclass(frozen=True)
class Deployed:
    """What one cluster's deploy rendered, and what happened to each namespace."""

    fleet: Fleet
    cluster: str
    files: Mapping[str, str]
    results: Results | None
    commit: str  # the main commit it was rendered from


_ATTEMPTS = 5


def publish(branch: Branch, deployed: Deployed) -> str | None:
    """Commit the cluster's part to the branch and push it. Returns the new commit, or None if nothing changed.

    Two runs can publish at once, one for each cluster: the pipeline's locks are per cluster, and the branch is shared.
    The push is never forced, so the slower one is refused; it then reads the branch again and puts its cluster's part
    on top of what the other published."""
    refused = 0
    while True:
        parent = gitio.remote_branch(branch.root, branch.remote, branch.name)
        previous = gitio.at_ref(branch.root, parent) if parent is not None else {}
        tree = published(deployed.fleet, deployed.cluster, deployed.files, previous, deployed.results)
        if parent is not None and tree == previous:
            return None
        states = sorted({item.state for item in deployed.results.namespaces.values()}) if deployed.results is not None else []
        message = f"{deployed.cluster}: rendered from {deployed.commit[:12]}" + (f" ({', '.join(states)})" if states else "")
        made = gitio.commit_files(branch.root, tree, parent=parent, message=message)
        if not branch.push:
            return made
        try:
            gitio.push(branch.root, branch.remote, made, branch.name)
        except gitio.GitError:
            refused += 1
            if refused == _ATTEMPTS or gitio.remote_branch(branch.root, branch.remote, branch.name) == parent:
                raise  # the branch did not move, so the push failed for another reason, such as permissions
        else:
            return made
