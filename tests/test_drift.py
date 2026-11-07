"""The decisions behind `berth deploy` and `berth drift`, on facts shaped like kubectl's output."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from berth.drift import BreakGlass, NamespaceDrift, Report, changed_objects, field_paths, hand_changes, prune_plan, removal_patch, slack, text, unmanaged
from berth.kube import ManagedFields, ObjectRef
from berth.operate import Target, select

NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)
BERTH = {"app.kubernetes.io/managed-by": "berth"}

# What `kubectl set env deployment/ledger-api LOG_LEVEL=debug` leaves in managedFields.
SET_ENV: dict[str, object] = {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"main"}': {"f:env": {'k:{"name":"LOG_LEVEL"}': {".": {}, "f:name": {}, "f:value": {}}}}}}}}}


def ref(kind: str, name: str, **fields: object) -> ObjectRef:
    """An object in ledger-dev, labelled as berth's unless `labels` says otherwise."""
    return replace(ObjectRef(kind, name, "ledger-dev", BERTH, {}, owned=False), **fields)


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


def test_the_removal_patch_deletes_an_added_list_item_by_its_key() -> None:
    patch = removal_patch(ref("Deployment", "ledger-api", managed_fields=(ManagedFields("kubectl-set", "Update", "", SET_ENV),)))
    assert patch == {"spec": {"template": {"spec": {"containers": [{"name": "main", "env": [{"name": "LOG_LEVEL", "$patch": "delete"}]}]}}}}


def test_a_custom_resource_cannot_be_patched_back_automatically() -> None:
    assert removal_patch(ref("HTTPRoute", "web", managed_fields=(ManagedFields("kubectl-edit", "Update", "", {"f:spec": {"f:hostnames": {}}}),))) is None


def test_prune_removes_only_what_berth_made_and_never_data() -> None:
    live = [
        ref("ConfigMap", "ledger-config-old"),
        ref("ConfigMap", "ledger-config-new"),
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


def test_changed_objects_are_read_from_kubectl_diff_file_names() -> None:
    diff = (
        "diff -u -N /tmp/LIVE-1/apps.v1.Deployment.ledger-dev.ledger-api /tmp/MERGED-1/apps.v1.Deployment.ledger-dev.ledger-api\n"
        "--- /tmp/LIVE-1/...\n"
        "diff -u -N /tmp/LIVE-1/v1.ConfigMap.ledger-dev.ledger-config.v2 /tmp/MERGED-1/v1.ConfigMap.ledger-dev.ledger-config.v2\n"
    )
    assert changed_objects(diff, "ledger-dev") == ["Deployment/ledger-api", "ConfigMap/ledger-config.v2"]


def test_break_glass_round_trips_and_expires() -> None:
    mark = BreakGlass(by="alice.ng@contoso.com", reason="load test", until=NOW + timedelta(hours=2))
    assert BreakGlass.decode(mark.encode()) == mark
    assert mark.active(NOW)
    assert not mark.active(NOW + timedelta(hours=3))


@pytest.mark.parametrize("value", ["not json", json.dumps({"by": "x"}), json.dumps({"until": "2026-11-09T14:00:00"})])  # the last has no time zone
def test_a_malformed_break_glass_mark_is_ignored(value: str) -> None:
    assert BreakGlass.decode(value) is None


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


TARGETS = [Target("ledger", "dev", "ledger-dev"), Target("ledger", "qa", "ledger-qa"), Target("payments-api", "dev", "payments-api-dev")]


@pytest.mark.parametrize(
    ("app", "env", "changed", "namespaces", "platform"),
    [
        (None, None, None, ["ledger-dev", "ledger-qa", "payments-api-dev"], True),
        ("ledger", None, None, ["ledger-dev", "ledger-qa"], False),
        ("ledger", "qa", None, ["ledger-qa"], False),
        (None, None, ["rendered/k8s/aks-qa/ledger-dev/manifests.yaml", "apps/ledger/dev.yaml"], ["ledger-dev"], False),
        (None, None, ["rendered/k8s/aks-qa/_platform/platform.yaml"], [], True),
        (None, None, ["platform/software/envoy-gateway.yaml"], [], True),
        (None, None, ["rendered/k8s/aks-prod/ledger-prod/manifests.yaml"], [], False),
    ],
)
def test_a_deploy_selects_one_app_what_changed_or_everything(app: str | None, env: str | None, changed: list[str] | None, namespaces: list[str], platform: bool) -> None:  # noqa: FBT001 - a parametrized expectation
    selection = select(TARGETS, "aks-qa", app=app, env=env, changed=changed)
    assert [t.namespace for t in selection.targets] == namespaces
    assert selection.platform is platform
