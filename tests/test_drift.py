"""The decisions behind `berth deploy` and `berth drift`, on facts shaped like the API server's answers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from support import fleet

from berth.drift import (
    SUPERSEDED_AT,
    BreakGlass,
    NamespaceDrift,
    Report,
    configmaps_used,
    field_paths,
    hand_changes,
    missing,
    object_diff,
    prune_brake,
    prune_plan,
    reclaims_replicas,
    removal_patch,
    replicas_to_hold,
    slack,
    superseded,
    text,
    unmanaged,
)
from berth.kube import ManagedFields, ObjectRef, rollout_state
from berth.model import Limit
from berth.operate import DeployOptions, Outcome, Plan, Results, Seen, State, Target, approval, decide

NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)
BERTH = {"app.kubernetes.io/managed-by": "berth"}

# What `kubectl set env deployment/ledger-api LOG_LEVEL=debug` leaves in managedFields.
SET_ENV: dict[str, object] = {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"main"}': {"f:env": {'k:{"name":"LOG_LEVEL"}': {".": {}, "f:name": {}, "f:value": {}}}}}}}}}


def ref(kind: str, name: str, **fields: object) -> ObjectRef:
    """An object in ledger-dev, labelled as berth's unless `labels` says otherwise."""
    return replace(ObjectRef(kind, name, "ledger-dev", BERTH, {}, owned=False, api_version="v1"), **fields)


def test_an_element_added_as_a_whole_is_one_path() -> None:
    assert field_paths(SET_ENV) == [".spec.template.spec.containers[name=main].env[name=LOG_LEVEL]"]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (ManagedFields("kubectl-set", "Update", "", SET_ENV), ["kubectl-set: .spec.template.spec.containers[name=main].env[name=LOG_LEVEL]"]),
        (ManagedFields("kubectl", "Update", "scale", {"f:spec": {"f:replicas": {}}}), ["kubectl: .spec.replicas"]),
        (ManagedFields("berth", "Apply", "", SET_ENV), []),
        (ManagedFields("kube-controller-manager", "Update", "scale", {"f:spec": {"f:replicas": {}}}), []),  # an autoscaler
        (ManagedFields("kubectl-edit", "Update", "status", {"f:status": {"f:replicas": {}}}), []),
        (ManagedFields("kubectl-rollout", "Update", "", {"f:spec": {"f:template": {"f:metadata": {"f:annotations": {"f:kubectl.kubernetes.io/restartedAt": {}}}}}}), []),
    ],
)
def test_only_changes_people_made_count_as_by_hand(entry: ManagedFields, expected: list[str]) -> None:
    assert hand_changes(ref("Deployment", "ledger-api", managed_fields=(entry,))) == expected


REPLICAS: dict[str, object] = {"f:spec": {"f:replicas": {}}}
FIXED: dict[str, object] = {"kind": "Deployment", "metadata": {"name": "ledger-api"}, "spec": {"replicas": 3}}
AUTOSCALED: dict[str, object] = {"kind": "Deployment", "metadata": {"name": "ledger-api"}, "spec": {}}


def _deployment(*owners: ManagedFields, replicas: int = 5) -> ObjectRef:
    return ref("Deployment", "ledger-api", managed_fields=owners, body={"spec": {"replicas": replicas}})


def test_a_deployment_that_starts_autoscaling_keeps_its_replicas_until_the_autoscaler_acts() -> None:
    ours = ManagedFields("berth", "Apply", "", REPLICAS)
    assert replicas_to_hold(AUTOSCALED, _deployment(ours)) == 5  # git lets go of the count: hold it, or it drops to one
    assert replicas_to_hold(FIXED, _deployment(ours)) is None  # git still sets it
    assert replicas_to_hold(AUTOSCALED, _deployment(ManagedFields("kube-controller-manager", "Update", "scale", REPLICAS))) is None  # already the autoscaler's
    assert replicas_to_hold(AUTOSCALED, None) is None  # a new Deployment: the autoscaler starts it at its minimum


@pytest.mark.parametrize(
    ("owners", "expected"),
    [
        ((ManagedFields("kube-controller-manager", "Update", "scale", REPLICAS),), True),  # the autoscaler moved it
        ((ManagedFields("berth-handover", "Apply", "", REPLICAS),), True),  # the autoscaler never had to
        ((ManagedFields("berth", "Apply", "", REPLICAS),), False),  # git's already: nothing to take back
        ((ManagedFields("kubectl", "Update", "scale", REPLICAS),), False),  # a person scaled it: that is a conflict to show
        ((ManagedFields("kube-controller-manager", "Update", "scale", REPLICAS), ManagedFields("kubectl-set", "Update", "", SET_ENV)), False),  # and so is any other hand change
    ],
)
def test_a_deployment_that_stops_autoscaling_takes_its_replicas_back_from_the_autoscaler_only(owners: tuple[ManagedFields, ...], expected: bool) -> None:  # noqa: FBT001 - a parametrized expectation
    assert reclaims_replicas(FIXED, _deployment(*owners)) is expected
    assert not reclaims_replicas(AUTOSCALED, _deployment(*owners))


def test_the_removal_patch_deletes_an_added_list_item_by_its_key() -> None:
    patch = removal_patch(ref("Deployment", "ledger-api", managed_fields=(ManagedFields("kubectl-set", "Update", "", SET_ENV),)))
    assert patch == {"spec": {"template": {"spec": {"containers": [{"name": "main", "env": [{"name": "LOG_LEVEL", "$patch": "delete"}]}]}}}}


def test_a_custom_resource_cannot_be_patched_back_automatically() -> None:
    assert removal_patch(ref("HTTPRoute", "web", managed_fields=(ManagedFields("kubectl-edit", "Update", "", {"f:spec": {"f:hostnames": {}}}),))) is None


def test_prune_removes_only_what_berth_made_and_never_data() -> None:
    live = [
        ref("ConfigMap", "ledger-config-old"),
        ref("ConfigMap", "ledger-config-new"),
        ref("ConfigMap", "ledger-config-1234", labels=BERTH | VERSIONED),  # retired by `superseded`, not pruned
        ref("PersistentVolumeClaim", "ledger-api-data"),
        ref("ConfigMap", "kept", annotations={"berth.contoso.io/keep": "true"}),
        ref("ConfigMap", "hand-made", labels={}),
        ref("Secret", "ledger-secrets", owned=True),
    ]
    rendered = [{"kind": "ConfigMap", "metadata": {"name": "ledger-config-new"}}]
    assert [r.name for r in prune_plan(rendered, live)] == ["ledger-config-old"]


def test_unmanaged_objects_exclude_what_kubernetes_and_controllers_make() -> None:
    live = [
        ref("ConfigMap", "tmp-fix", labels={}),
        ref("ConfigMap", "kube-root-ca.crt", labels={}),
        ref("ServiceAccount", "default", labels={}),
        ref("Pod", "ledger-api-abc", labels={}, owned=True),
        ref("Deployment", "ledger-api"),
    ]
    assert [r.name for r in unmanaged(live)] == ["tmp-fix"]


def test_an_object_diff_ignores_what_the_server_keeps_for_itself() -> None:
    live = {"kind": "Deployment", "metadata": {"name": "api", "resourceVersion": "7", "managedFields": [{}]}, "spec": {"replicas": 4}, "status": {"replicas": 4}}
    same = {"kind": "Deployment", "metadata": {"name": "api", "resourceVersion": "9"}, "spec": {"replicas": 4}}
    wanted = {"kind": "Deployment", "metadata": {"name": "api"}, "spec": {"replicas": 1}}
    assert object_diff("Deployment/api", live, same) == ""
    changed = object_diff("Deployment/api", live, wanted)
    assert "-  replicas: 4" in changed
    assert "+  replicas: 1" in changed
    assert object_diff("Deployment/api", None, wanted).startswith("--- live/Deployment/api")


def test_missing_objects_are_what_git_renders_and_the_cluster_lacks() -> None:
    rendered = [{"kind": "Namespace", "metadata": {"name": "ledger-dev"}}, {"kind": "Deployment", "metadata": {"name": "ledger-api"}}, {"kind": "Service", "metadata": {"name": "api"}}]
    assert missing(rendered, [ref("Service", "api")]) == ["Deployment/ledger-api"]


def test_break_glass_round_trips_and_expires() -> None:
    mark = BreakGlass(by="alice.ng@contoso.com", reason="load test", until=NOW + timedelta(hours=2))
    assert BreakGlass.from_data(mark.data()) == mark
    assert mark.active(NOW)
    assert not mark.active(NOW + timedelta(hours=3))


LIMIT = Limit(share=0.05, floor=20)


def _planned(count: int, kind: str = "CronJob") -> dict[str, list[ObjectRef]]:
    return {f"app{i}-dev": [ref(kind, f"app{i}-cleanup", namespace=f"app{i}-dev")] for i in range(count)}


@pytest.mark.parametrize(("count", "whole", "brakes"), [(20, 100, False), (21, 100, True), (50, 1000, False), (51, 1000, True), (0, 0, False)])
def test_a_run_that_would_delete_a_large_share_of_a_cluster_deletes_nothing(count: int, whole: int, brakes: bool) -> None:  # noqa: FBT001 - parametrized expectation
    assert bool(prune_brake(_planned(count), whole, LIMIT)) is brakes


def test_the_prune_brake_says_which_kinds_would_go_and_how_to_proceed() -> None:
    lines = prune_brake(_planned(30) | {"ledger-dev": [ref("NetworkPolicy", "a"), ref("NetworkPolicy", "b")]}, 300, LIMIT)
    assert lines[0].startswith("prune brake: this run would delete 32 of the 300 objects berth made in app namespaces")
    assert "  CronJob: 30 in 30 namespaces" in lines
    assert "  NetworkPolicy: 2 in 1 namespace" in lines
    assert "--prune-all" in lines[-1]
    assert "--no-prune" in lines[-1]


@pytest.mark.parametrize("data", [{}, {"by": "x"}, {"until": "2026-11-09T14:00:00"}, {"until": "soon"}])  # the third has no time zone
def test_a_malformed_break_glass_mark_is_ignored(data: dict[str, object]) -> None:
    assert BreakGlass.from_data(data) is None


def test_the_report_counts_and_explains_each_namespace_that_needs_attention() -> None:
    expired = BreakGlass(by="alice.ng@contoso.com", reason="incident", until=NOW - timedelta(hours=1))
    report = Report(
        "aks-qa",
        NOW,
        (
            NamespaceDrift("payments-api-dev", "payments-api", "dev"),
            NamespaceDrift("ledger-dev", "ledger", "dev", by_hand=("Deployment/ledger-api: kubectl-set: .spec.replicas",), unmanaged=("ConfigMap/tmp-fix",)),
            NamespaceDrift("risk-batch-dev", "risk-batch", "dev", changed=("Deployment/risk-batch-var",), break_glass=expired),
        ),
    )
    summary = text(report)
    assert summary.startswith("berth drift on aks-qa, 2026-11-09 14:00 UTC: 1 in sync, 2 need attention")
    assert "ledger-dev: changed by hand: Deployment/ledger-api: kubectl-set: .spec.replicas; made by hand: ConfigMap/tmp-fix; not marked" in summary
    assert "break-glass by alice.ng@contoso.com EXPIRED" in summary
    assert slack([report]) == {"text": summary.rstrip()}


def test_an_in_sync_report_still_says_it_ran() -> None:
    assert text(Report("aks-prod", NOW, (NamespaceDrift("ledger-prod", "ledger", "prod"),))) == "berth drift on aks-prod, 2026-11-09 14:00 UTC: 1 in sync, 0 need attention\n"


FLEET = fleet()
OPTIONS = DeployOptions(commit="c" * 40, now=NOW)


def _target(env: str, digest: str = "new") -> Target:
    return Target("demo", env, f"demo-{env}", f"k8s/aks-qa/demo-{env}/manifests.yaml", digest)


def _seen(env: str, digest: str | None = "old", *, commit: str | None = None, mark: BreakGlass | None = None) -> Seen:
    annotations = {"berth.contoso.io/deployed-digest": digest} if digest is not None else {}
    annotations |= {"berth.contoso.io/deployed-commit": commit} if commit is not None else {}
    namespace = ObjectRef("Namespace", f"demo-{env}", None, BERTH, annotations, owned=False)
    return Seen({namespace.name: namespace}, {namespace.name: mark} if mark is not None else {}, {})


def _decided(target: Target, seen: Seen, outcomes: dict[tuple[str, str], Outcome] | None = None, options: DeployOptions = OPTIONS) -> State | None:
    found = decide(target, seen, outcomes or {}, FLEET, options)
    return None if found is None else found.state


def test_a_namespace_that_runs_what_git_renders_is_left_alone() -> None:
    assert _decided(_target("dev", "same"), _seen("dev", "same")) is State.UNCHANGED
    assert _decided(_target("dev", "new"), _seen("dev", "old")) is None  # deploy it
    assert _decided(_target("dev", "new"), Seen({}, {}, {})) is None  # a new namespace
    assert _decided(_target("dev", "same"), _seen("dev", "same"), options=replace(OPTIONS, revert=True)) is None  # reverting reapplies


@pytest.mark.parametrize(("source", "held"), [(State.FAILED, True), (State.HELD, True), (State.DEPLOYED, False), (State.UNCHANGED, False), (State.SKIPPED, False)])
def test_an_environment_is_held_while_the_one_it_promotes_from_failed(source: State, held: bool) -> None:  # noqa: FBT001 - parametrized expectation
    outcomes = {("demo", "qa"): Outcome(app="demo", env="qa", state=source)}
    assert (_decided(_target("prod"), _seen("prod"), outcomes) is State.HELD) is held
    assert _decided(_target("prod"), _seen("prod"), {("other", "qa"): Outcome(app="other", env="qa", state=State.FAILED)}) is None  # another app never holds this one


def test_prod_is_held_while_qa_is_skipped_for_break_glass_but_not_for_a_newer_commit() -> None:
    glass = Outcome(app="demo", env="qa", state=State.SKIPPED, detail="break-glass by alice", break_glass=True)
    held = decide(_target("prod"), _seen("prod"), {("demo", "qa"): glass}, FLEET, OPTIONS)
    assert held is not None
    assert held.state is State.HELD
    assert held.detail == "demo was skipped for break-glass in qa, and prod promotes from qa"
    newer = Outcome(app="demo", env="qa", state=State.SKIPPED, detail="a newer commit is already deployed here")
    assert _decided(_target("prod"), _seen("prod"), {("demo", "qa"): newer}) is None


def test_an_environment_deployed_on_its_own_is_held_unless_its_source_runs_what_git_renders() -> None:
    target = replace(_target("qa"), source="demo-dev", source_digest="dev-now")
    both = Seen({**_seen("qa").namespaces, **_seen("dev", "dev-now").namespaces}, {}, {})
    assert _decided(target, both) is None  # dev runs what git renders for it
    stale = Seen({**_seen("qa").namespaces, **_seen("dev", "dev-before").namespaces}, {}, {})
    held = decide(target, stale, {}, FLEET, OPTIONS)
    assert held is not None
    assert held.state is State.HELD
    assert held.detail == "demo-dev does not run what git renders for it and is not deployed in this run, and qa promotes from dev"
    assert _decided(target, _seen("qa")) is State.HELD  # dev was never deployed
    assert _decided(target, stale, {("demo", "dev"): Outcome(app="demo", env="dev", state=State.DEPLOYED)}) is None  # this run deployed it


def test_break_glass_and_newer_commits_are_skipped() -> None:
    mark = BreakGlass(by="alice", reason="incident", until=NOW + timedelta(hours=1))
    assert _decided(_target("dev"), _seen("dev", mark=mark)) is State.SKIPPED
    assert _decided(_target("dev"), _seen("dev", mark=replace(mark, until=NOW - timedelta(hours=1)))) is None  # expired
    assert _decided(_target("dev"), _seen("dev", mark=mark), options=replace(OPTIONS, include_break_glass=True)) is None
    newer = replace(OPTIONS, newer=lambda commit: commit == "d" * 40)
    assert _decided(_target("dev"), _seen("dev", commit="d" * 40), options=newer) is State.SKIPPED
    assert _decided(_target("dev"), _seen("dev", commit="e" * 40), options=newer) is None


def _prod_plan(changing: int, of: int, platform: tuple[str, ...] = ()) -> tuple[Plan, list[Target]]:
    every = [replace(_target("prod"), app=f"app{i}", namespace=f"app{i}-prod") for i in range(of)]
    states = [State.DEPLOYED] * changing + [State.UNCHANGED] * (of - changing)
    namespaces = {target.namespace: Outcome(app=target.app, env="prod", state=state) for target, state in zip(every, states, strict=True)}
    return Plan(cluster="aks-prod", commit="c" * 40, platform=list(platform), namespaces=namespaces), every


@pytest.mark.parametrize(("changing", "of", "asks"), [(1, 400, False), (40, 400, False), (41, 400, True), (5, 6, False), (6, 6, True)])
def test_prod_waits_for_a_person_when_many_app_environments_change_at_once(changing: int, of: int, asks: bool) -> None:  # noqa: FBT001 - parametrized expectation
    planned, every = _prod_plan(changing, of)
    assert bool(approval(planned, FLEET, every)) is asks


def test_prod_waits_for_a_person_when_the_platform_would_change_but_qa_never_does() -> None:
    planned, every = _prod_plan(1, 400, platform=("ClusterRole/berth-break-glass",))
    assert approval(planned, FLEET, every) == ["the platform would change on aks-prod: ClusterRole/berth-break-glass"]
    on_qa = planned.model_copy(update={"cluster": "aks-qa"})
    assert approval(on_qa, FLEET, every) == []  # aks-qa runs no production environment


@pytest.mark.parametrize(
    ("platform", "states", "code"),
    [("deployed", [State.DEPLOYED, State.UNCHANGED, State.SKIPPED], 0), ("deployed", [State.DEPLOYED, State.HELD], 2), ("failed", [], 1), ("skipped", [State.FAILED], 2)],
)
def test_the_exit_code_separates_a_failed_platform_from_failed_apps(platform: str, states: list[State], code: int) -> None:
    namespaces = {f"app{i}-dev": Outcome(app=f"app{i}", env="dev", state=state) for i, state in enumerate(states)}
    results = Results.model_validate({"cluster": "aks-qa", "commit": "c", "platform": platform, "namespaces": namespaces})
    assert results.exit_code == code
    assert Results.model_validate_json(results.model_dump_json()) == results


VERSIONED = {"berth.contoso.io/versioned": "true"}


def test_superseded_settings_are_noted_then_deleted_after_the_grace_period() -> None:
    def versioned(name: str, since: datetime | None = None) -> ObjectRef:
        return ref("ConfigMap", name, labels=BERTH | VERSIONED, annotations={SUPERSEDED_AT: since.isoformat()} if since is not None else {})

    live = [
        versioned("cfg-current"),
        versioned("cfg-just-replaced"),
        versioned("cfg-replaced-last-week", NOW - timedelta(days=8)),
        versioned("cfg-replaced-yesterday", NOW - timedelta(days=1)),
        versioned("cfg-back-again", NOW - timedelta(days=30)),  # a revert rendered it again
        ref("ConfigMap", "hand-made", labels={}),
    ]
    plan = superseded(live, {"cfg-current", "cfg-back-again"}, NOW)
    assert [item.name for item in plan.mark] == ["cfg-just-replaced"]
    assert [item.name for item in plan.unmark] == ["cfg-back-again"]
    assert [item.name for item in plan.delete] == ["cfg-replaced-last-week"]


def test_a_config_map_a_live_workload_names_counts_as_in_use() -> None:
    template = {"template": {"spec": {"containers": [{"envFrom": [{"configMapRef": {"name": "cfg-a"}}]}], "volumes": [{"name": "f", "configMap": {"name": "cfg-b"}}]}}}
    deployment = ref("Deployment", "ledger-api", body={"spec": template})
    assert configmaps_used([deployment, ref("Service", "api", body={"spec": template})]) == {"cfg-a", "cfg-b"}


@pytest.mark.parametrize(
    ("status", "done", "why"),
    [
        ({"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "availableReplicas": 2}, True, ""),
        ({"observedGeneration": 1, "replicas": 2, "updatedReplicas": 2, "availableReplicas": 2}, False, "waiting for the Deployment controller"),
        ({"observedGeneration": 2, "replicas": 3, "updatedReplicas": 2, "availableReplicas": 2}, False, "1 old pods still running"),
        ({"observedGeneration": 2, "replicas": 2, "updatedReplicas": 2, "availableReplicas": 1}, False, "1 of 2 new pods ready"),
        ({"observedGeneration": 2, "conditions": [{"type": "Progressing", "reason": "ProgressDeadlineExceeded", "message": "too slow"}]}, False, "failed: too slow"),
    ],
)
def test_rollouts_finish_by_the_rules_kubectl_uses(status: dict[str, object], done: bool, why: str) -> None:  # noqa: FBT001 - parametrized expectation
    assert rollout_state({"metadata": {"generation": 2}, "spec": {"replicas": 2}, "status": status}) == (done, why)


def test_a_change_adopted_into_git_is_no_longer_by_hand() -> None:
    # Someone scaled to 4, then the spec said 4 too: berth now co-owns .spec.replicas.
    scaled = ManagedFields("kubectl", "Update", "scale", {"f:spec": {"f:replicas": {}}})
    applied = ManagedFields("berth", "Apply", "", {"f:spec": {"f:replicas": {}, "f:template": {}}})
    adopted = ref("Deployment", "payments-api-api", managed_fields=(scaled, applied))
    assert hand_changes(adopted) == []
    assert removal_patch(adopted) is None  # --revert must not delete a field git sets


def _env(*items: tuple[str, dict[str, object]]) -> dict[str, object]:
    """A fieldsV1 tree for environment variables of the container `main`."""
    env = {f'k:{{"name":"{name}"}}': fields for name, fields in items}
    return {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"main"}': {"f:env": env}}}}}}


def test_revert_removes_only_what_git_does_not_also_set() -> None:
    whole: dict[str, object] = {".": {}, "f:name": {}, "f:value": {}}
    edited = ManagedFields("kubectl-edit", "Update", "", _env(("BERTH_ENV", {"f:value": {}}), ("EXTRA", whole)))
    applied = ManagedFields("berth", "Apply", "", _env(("BERTH_ENV", whole)))
    patch = removal_patch(ref("Deployment", "ledger-api", managed_fields=(edited, applied)))
    assert patch == {"spec": {"template": {"spec": {"containers": [{"name": "main", "env": [{"name": "EXTRA", "$patch": "delete"}]}]}}}}
