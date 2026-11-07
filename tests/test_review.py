from datetime import UTC, datetime

import pytest
from support import OTHER_PLATFORM_APPROVER, OWNERS, PLATFORM_APPROVER, RELEASE_BOT, app_spec, env_spec, fleet, version

from berth.diff import Change
from berth.model import ReviewPolicy, Rule
from berth.review import BREAK_GLASS, Decision, Proposal, Requirement, Tier, Votes, classify, decide, matches, requirements

FLEET = fleet()
PLATFORM, POLICY = FLEET.platform, FLEET.policy
OUTSIDE_FREEZE = datetime(2026, 11, 7, 12, tzinfo=UTC)
IN_FREEZE = datetime(2026, 12, 30, 12, tzinfo=UTC)
ANN, BEN = OWNERS
STRANGER = "zoe@contoso.com"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("services.*.size", "services.api.size", True),
        ("services.*.size", "services.api.extra.size", False),
        ("workflows.**", "workflows.nightly.tasks.load.size", True),
        ("services.*.health.**", "services.api.health", True),
        ("version", "version.tag", False),
        ("config.*", "config", False),
    ],
)
def test_path_patterns(pattern: str, path: str, expected: bool) -> None:  # noqa: FBT001 - parametrized expectation
    assert matches(pattern, path) is expected


@pytest.mark.parametrize(
    ("env", "path", "before", "after", "tier"),
    [
        # App settings are the owner's call: free in dev and qa, a second person in prod.
        ("dev", "config.LOG_LEVEL", "info", "debug", Tier.AUTO),
        ("prod", "config.LOG_LEVEL", "info", "debug", Tier.OWNER),
        ("qa", "services.api.size", "s", "m", Tier.AUTO),
        ("prod", "workflows.nightly.schedule", "0 1 * * *", "0 2 * * *", Tier.OWNER),
        # Escalations that look at the value, in any environment.
        ("dev", "services.api.size", "m", "xl", Tier.PLATFORM),
        ("prod", "services.api.replicas", 3, 10, Tier.OWNER),
        ("prod", "services.api.replicas", 3, 11, Tier.PLATFORM),
        ("qa", "services.api.autoscale.max", 4, 11, Tier.PLATFORM),
        ("dev", "services.api.expose", "internal", "company", Tier.AUTO),
        ("prod", "services.api.expose", "internal", "company", Tier.OWNER),
        # Fields that are platform territory however small the change.
        ("dev", "services.api.hostname", None, "portal", Tier.AUTO),
        ("prod", "services.api.hostname", "portal", "portal-new", Tier.OWNER),
        ("dev", "services.api.volumes.data.size_gi", 10, 20, Tier.PLATFORM),
        ("dev", "azure_identity", False, True, Tier.PLATFORM),
        ("dev", "overrides", None, "[...]", Tier.PLATFORM),
        ("prod", "environment", None, "added", Tier.PLATFORM),
        ("dev", "environment", None, "added", Tier.AUTO),
        # A path nobody wrote a rule for fails safe.
        ("dev", "something.new", None, 1, Tier.PLATFORM),
        ("staging", "config.X", None, "1", Tier.PLATFORM),
        (None, "owners", (ANN, BEN), (ANN, STRANGER), Tier.OWNER),
        (None, "app", None, "added", Tier.OWNER),
    ],
)
def test_classify_tiers(env: str | None, path: str, before: object, after: object, tier: Tier) -> None:
    assert classify(Change("demo", env, path, before, after), PLATFORM, POLICY, {}).tier is tier


def test_files_outside_apps_need_platform_review() -> None:
    assert classify(Change(None, None, "platform_file.platform/platform.yaml", None, "x"), PLATFORM, POLICY, {}).tier is Tier.PLATFORM


@pytest.mark.parametrize(
    ("env", "history", "tier"),
    [
        ("prod", {("demo", "qa"): {version("1.4.0")}}, Tier.OWNER),  # ran in qa first
        ("prod", {("demo", "qa"): {version("1.3.0")}}, Tier.PLATFORM),  # never ran in qa
        ("prod", {}, Tier.PLATFORM),
        ("qa", {("demo", "dev"): {version("1.4.0")}}, Tier.AUTO),
        ("dev", {}, Tier.AUTO),  # dev promotes from nothing
    ],
)
def test_version_must_have_run_in_the_previous_environment(env: str, history: dict[tuple[str, str], set[str]], tier: Tier) -> None:
    change = Change("demo", env, "version", version("1.3.0"), version("1.4.0"))
    assert classify(change, PLATFORM, POLICY, history).tier is tier


def _req(tier: Tier, app: str | None = "demo", path: str = "config.X", env: str | None = "dev") -> Requirement:
    change = Change(app, env, path, "a", "b")
    return Requirement(app, tier, OWNERS if app is not None else (), (classify(change, PLATFORM, POLICY, {}),))


def _proposal(author: str, approved: tuple[str, ...] = (), rejected: tuple[str, ...] = (), labels: tuple[str, ...] = ()) -> Proposal:
    return Proposal(author, Votes(frozenset(approved), frozenset(rejected)), frozenset(labels))


def _decide(req: Requirement, proposal: Proposal, now: datetime = OUTSIDE_FREEZE) -> Decision:
    return decide([req], proposal, PLATFORM, now)


@pytest.mark.parametrize(
    ("req", "author", "approved", "state"),
    [
        (_req(Tier.AUTO), ANN, (), "succeeded"),  # an owner may self-merge auto changes
        (_req(Tier.AUTO), STRANGER, (), "pending"),  # anyone else needs an owner
        (_req(Tier.AUTO), STRANGER, (BEN,), "succeeded"),
        (_req(Tier.OWNER), ANN, (ANN,), "pending"),  # approving your own change does not count
        (_req(Tier.OWNER), ANN, (BEN,), "succeeded"),
        (_req(Tier.PLATFORM), ANN, (BEN,), "pending"),
        (_req(Tier.PLATFORM), ANN, (PLATFORM_APPROVER,), "pending"),  # platform does not replace the owner
        (_req(Tier.PLATFORM), ANN, (BEN, PLATFORM_APPROVER), "succeeded"),
        (_req(Tier.PLATFORM, app=None, path="platform_file.x", env=None), PLATFORM_APPROVER, (PLATFORM_APPROVER,), "pending"),
        (_req(Tier.PLATFORM, app=None, path="platform_file.x", env=None), PLATFORM_APPROVER, (OTHER_PLATFORM_APPROVER,), "succeeded"),
    ],
)
def test_gate_approvals(req: Requirement, author: str, approved: tuple[str, ...], state: str) -> None:
    assert _decide(req, _proposal(author, approved)).state == state


def test_a_rejection_by_an_owner_blocks_even_with_approvals() -> None:
    assert _decide(_req(Tier.OWNER), _proposal(ANN, approved=(BEN, PLATFORM_APPROVER), rejected=(BEN,))).state == "failed"


def test_a_rejection_by_a_stranger_does_not_block() -> None:
    assert _decide(_req(Tier.OWNER), _proposal(ANN, approved=(BEN,), rejected=(STRANGER,))).state == "succeeded"


def test_release_bot_may_change_versions_only() -> None:
    assert _decide(_req(Tier.AUTO, path="version"), _proposal(RELEASE_BOT)).state == "succeeded"
    assert _decide(_req(Tier.AUTO, path="config.X"), _proposal(RELEASE_BOT)).state == "failed"


@pytest.mark.parametrize(
    ("env", "now", "labels", "approved", "state"),
    [
        ("prod", IN_FREEZE, (), (BEN,), "pending"),
        ("prod", OUTSIDE_FREEZE, (), (BEN,), "succeeded"),
        ("dev", IN_FREEZE, (), (), "succeeded"),  # freezes only hold production
        ("prod", IN_FREEZE, (BREAK_GLASS,), (BEN,), "succeeded"),
        ("prod", IN_FREEZE, (BREAK_GLASS,), (), "pending"),
    ],
)
def test_freezes_and_break_glass(env: str, now: datetime, labels: tuple[str, ...], approved: tuple[str, ...], state: str) -> None:
    tier = Tier.OWNER if env == "prod" else Tier.AUTO
    assert _decide(_req(tier, env=env), _proposal(ANN, approved=approved, labels=labels), now).state == state


def test_break_glass_still_needs_someone_other_than_the_author() -> None:
    assert _decide(_req(Tier.PLATFORM, env="prod"), _proposal(PLATFORM_APPROVER, approved=(PLATFORM_APPROVER,), labels=(BREAK_GLASS,))).state == "pending"


def test_owners_come_from_the_target_branch_so_nobody_can_add_themselves_and_approve() -> None:
    base = fleet()
    head = fleet({"demo": (app_spec(owners=[ANN, STRANGER]), {"dev": env_spec(), "qa": env_spec(), "prod": env_spec()})})
    finding = classify(Change("demo", None, "owners", OWNERS, (ANN, STRANGER)), PLATFORM, POLICY, {})
    (req,) = requirements([finding], base, head)
    assert req.owners == OWNERS
    decision = decide([req], Proposal(STRANGER, Votes(frozenset({STRANGER}), frozenset()), frozenset()), PLATFORM, OUTSIDE_FREEZE)
    assert decision.state == "pending"


def test_a_new_app_needs_a_co_owner_and_the_platform_team() -> None:
    head = fleet({"demo": (app_spec(), {"dev": env_spec()}), "fresh": (app_spec(name="fresh"), {"dev": env_spec()})})
    finding = classify(Change("fresh", None, "app", None, "added"), PLATFORM, POLICY, {})
    (req,) = requirements([finding], fleet(), head)
    assert req.owners == OWNERS
    only_platform = decide([req], Proposal(ANN, Votes(frozenset({PLATFORM_APPROVER}), frozenset()), frozenset()), PLATFORM, OUTSIDE_FREEZE)
    both = decide([req], Proposal(ANN, Votes(frozenset({PLATFORM_APPROVER, BEN}), frozenset()), frozenset()), PLATFORM, OUTSIDE_FREEZE)
    assert (only_platform.state, both.state) == ("pending", "succeeded")


def test_production_never_drops_below_owner_even_if_the_table_says_auto() -> None:
    loose = ReviewPolicy(rules=(Rule(reason="loosened", paths=("config.*",), tier={"dev": "auto", "qa": "auto", "prod": "auto"}),))
    assert classify(Change("demo", "prod", "config.X", "a", "b"), PLATFORM, loose, {}).tier is Tier.OWNER
    assert classify(Change("demo", "dev", "config.X", "a", "b"), PLATFORM, loose, {}).tier is Tier.AUTO
