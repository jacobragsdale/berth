"""Decide who must approve a change, from what it changes, and whether the votes so far satisfy that.

Review effort follows risk: most edits need only the app's owners, some need nobody beyond an owner
authoring them, and a short list needs the platform team. Paths no rule covers need the platform team.
"""

from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Literal

from berth.diff import Change
from berth.fleet import Fleet
from berth.model import Platform, ReviewPolicy

BREAK_GLASS = "break-glass"


class Tier(IntEnum):
    AUTO = 0
    OWNER = 1
    PLATFORM = 2


_TIERS: Mapping[str, Tier] = {"auto": Tier.AUTO, "owner": Tier.OWNER, "platform": Tier.PLATFORM}


@dataclass(frozen=True)
class Finding:
    change: Change
    tier: Tier
    reason: str


type History = Mapping[tuple[str, str], AbstractSet[str]]
"""Versions each (app, environment) has had on the target branch. Promotion checks read it."""


def matches(pattern: str, path: str) -> bool:
    """Dotted-path glob: `*` is one segment, `**` is any number of segments (including none)."""

    def walk(pat: list[str], parts: list[str]) -> bool:
        if not pat:
            return not parts
        if pat[0] == "**":
            return any(walk(pat[1:], parts[index:]) for index in range(len(parts) + 1))
        return bool(parts) and pat[0] in {"*", parts[0]} and walk(pat[1:], parts[1:])

    return walk(pattern.split("."), path.split("."))


def _rule_tier(change: Change, policy: ReviewPolicy) -> tuple[Tier, str]:
    for rule in policy.rules:
        if any(matches(pattern, change.path) for pattern in rule.paths):
            if isinstance(rule.tier, str):
                return _TIERS[rule.tier], rule.reason
            if change.env is None:
                return max(_TIERS[tier] for tier in rule.tier.values()), rule.reason
            return _TIERS[rule.tier[change.env]], rule.reason
    return Tier.PLATFORM, "no review rule covers this path"


def classify(change: Change, platform: Platform, policy: ReviewPolicy, history: History) -> Finding:
    if change.env is not None and change.env not in platform.environments:
        return Finding(change, Tier.PLATFORM, f"environment {change.env} is not on the target branch yet")
    tier, reason = _rule_tier(change, policy)
    after, path = change.after, change.path
    escalations: list[str] = []
    if path.endswith(".size") and isinstance(after, str) and after in platform.sizes and platform.sizes[after].platform_review:
        escalations.append(f"size {after} needs platform review")
    if path.endswith((".replicas", ".autoscale.max")) and isinstance(after, int) and after > platform.max_replicas_without_review:
        escalations.append(f"more than {platform.max_replicas_without_review} replicas")
    if path.endswith(".expose") and after == "public":
        escalations.append("exposes a service outside the cluster")
    if path == "version" and change.app is not None and change.env is not None and isinstance(after, str):
        source = platform.environments[change.env].promotes_from
        if source is not None and after not in history.get((change.app, source), set()):
            escalations.append(f"{after.split('@', 1)[0]} has not run in {source}")
    if escalations:
        return Finding(change, Tier.PLATFORM, "; ".join(escalations))
    if change.env is not None and platform.environments[change.env].production and tier < Tier.OWNER:
        return Finding(change, Tier.OWNER, f"{reason} (production always needs a second person)")
    return Finding(change, tier, reason)


@dataclass(frozen=True)
class Requirement:
    """What one app (or the platform, when `app` is None) needs before the change can merge."""

    app: str | None
    tier: Tier
    owners: tuple[str, ...]
    findings: tuple[Finding, ...]


def requirements(findings: Iterable[Finding], base: Fleet, head: Fleet) -> list[Requirement]:
    grouped: dict[str | None, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.change.app, []).append(finding)
    result: list[Requirement] = []
    for app in sorted(grouped, key=lambda name: (name is None, name or "")):
        items = grouped[app]
        # Owners as of the target branch decide, so nobody can add themselves as an owner and then approve.
        source = None if app is None else (base.apps.get(app) or head.apps.get(app))
        owners = () if source is None else source.spec.owners
        result.append(Requirement(app, max(item.tier for item in items), owners, tuple(items)))
    return result


@dataclass(frozen=True)
class Votes:
    approved: frozenset[str]
    rejected: frozenset[str]


@dataclass(frozen=True)
class Proposal:
    """The pull request as the gate sees it: who opened it, how people voted, and its labels."""

    author: str
    votes: Votes
    labels: frozenset[str]


@dataclass(frozen=True)
class Decision:
    state: Literal["succeeded", "pending", "failed"]
    lines: tuple[str, ...]

    @property
    def summary(self) -> str:
        return self.lines[0] if self.lines else self.state


def _names(people: Iterable[str]) -> str:
    return ", ".join(sorted(people)) or "nobody (add a second owner)"


def _freezes(reqs: list[Requirement], platform: Platform, now: datetime) -> list[str]:
    production = any(env is not None and env in platform.environments and platform.environments[env].production for r in reqs for f in r.findings if (env := f.change.env) is not None)
    if not production:
        return []
    return [
        f"production change freeze until {freeze.end.isoformat()}: {freeze.reason} (label the PR {BREAK_GLASS} for an emergency)" for freeze in platform.freezes if freeze.start <= now < freeze.end
    ]


def _unmet(req: Requirement, proposal: Proposal, platform: Platform) -> list[str]:
    author, approved = proposal.author, proposal.votes.approved
    approvers = set(platform.platform_approvers) - {author}
    pool = set(req.owners) - {author}
    tier = req.tier
    if tier is Tier.AUTO and author not in req.owners and author != platform.release_bot:
        tier = Tier.OWNER  # auto means "an owner may self-merge", not "anyone may"
    unmet: list[str] = []
    if tier >= Tier.OWNER and req.app is not None and not approved & pool:
        unmet.append(f"an owner other than the author ({_names(pool)})")
    if tier is Tier.PLATFORM and not approved & approvers:
        unmet.append(f"the platform team ({_names(approvers)})")
    return unmet


def decide(reqs: list[Requirement], proposal: Proposal, platform: Platform, now: datetime) -> Decision:
    """The merge gate. `failed` blocks until a vote or the change itself changes; `pending` waits for approvals."""
    approvers = set(platform.platform_approvers)
    if proposal.author == platform.release_bot:
        stray = sorted({f.change.path for r in reqs for f in r.findings if f.change.path != "version"})
        if stray:
            return Decision("failed", (f"the release bot may only change versions, but this changes {', '.join(stray)}",))
    rejected = [f"{r.app or 'platform files'}: rejected by {_names(who)}" for r in reqs if (who := proposal.votes.rejected & (set(r.owners) | approvers))]
    if rejected:
        return Decision("failed", tuple(rejected))

    if BREAK_GLASS in proposal.labels:
        anyone = (approvers | {owner for r in reqs for owner in r.owners}) - {proposal.author}
        if proposal.votes.approved & anyone:
            return Decision("succeeded", ("break-glass: approved by one other person; a platform review after the incident is required",))
        return Decision("pending", ("break-glass: needs approval from one owner or platform approver other than the author",))

    pending = _freezes(reqs, platform, now)
    satisfied: list[str] = []
    for req in reqs:
        name = req.app or "platform files"
        if unmet := _unmet(req, proposal, platform):
            pending.append(f"{name}: needs approval from {' and '.join(unmet)}")
        else:
            satisfied.append(f"{name}: {req.tier.name.lower()} review satisfied")
    if pending:
        return Decision("pending", tuple(pending + satisfied))
    return Decision("succeeded", tuple(satisfied) or ("nothing to review",))
