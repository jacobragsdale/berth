"""`berth deploy`, `drift` and `breakglass` against a real kube-apiserver, and the policies and roles it enforces.

Integration tier: needs the envtest binaries (scripts/fetch-tools.sh) and network access once, to fetch the CRDs
from the platform software charts. The API server stores and admits objects; nothing runs, so rollouts are not
waited for (`--no-wait`).
"""

import io
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apiserver import BIN, TOKEN, apiserver
from support import REPO

from berth import gitio
from berth.cli import parse, run
from berth.fleet import load
from berth.kube import helm_template
from berth.operate import Results, State
from berth.render.software import releases, template_args, values

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)
DEVELOPERS = "77777777-7777-7777-7777-777777777777"  # dev's break-glass group in platform.yaml


@dataclass(frozen=True)
class Cluster:
    repo: Path
    kubeconfig: Path

    def berth(self, *argv: str, now: datetime = NOW, kubeconfig: Path | None = None) -> tuple[int, str]:
        out = io.StringIO()
        connection = ["--kubeconfig", str(kubeconfig or self.kubeconfig)] if argv[0] in {"deploy", "drift", "breakglass"} else []
        code = run(parse(["--root", str(self.repo), *argv, *connection]), out, now)
        return code, out.getvalue()

    def kubectl(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN / "kubectl"), f"--kubeconfig={self.kubeconfig}", "--context", "aks-qa", *args], input=stdin, capture_output=True, text=True, check=False)

    def drift(self) -> str:
        code, out = self.berth("drift", "--cluster", "aks-qa")
        assert code == 0, out
        return out

    def commit(self, message: str) -> None:
        _git(self.repo, "commit", "-qam", message)


def _git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@contoso.com", *args], check=True, capture_output=True, env=env)  # noqa: S607


def _crds(repo: Path) -> str:
    """The CRDs platform software brings (External Secrets, Gateway API), without running its controllers."""
    fleet = load(gitio.worktree(repo))
    docs: list[str] = []
    for release in releases(fleet.software, "aks-qa"):
        rendered = helm_template(str(BIN / "helm"), template_args(release, "1.34.1"), values(release, fleet.platform))
        docs += [doc for doc in rendered.split("\n---") if "kind: CustomResourceDefinition" in doc]
    return "\n---".join(docs)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    if not (BIN / "kube-apiserver").exists():
        pytest.fail("run scripts/fetch-tools.sh first")
    work = tmp_path_factory.mktemp("cluster")
    repo = work / "repo"
    for part in ("platform", "apps"):
        shutil.copytree(REPO / part, repo / part)
    shutil.copy(REPO / ".gitignore", repo / ".gitignore")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("BERTH_KUBECTL", str(BIN / "kubectl"))
        env.setenv("BERTH_HELM", str(BIN / "helm"))
        for name in ("CACHE", "CONFIG", "DATA"):
            env.setenv(f"HELM_{name}_HOME", str(REPO / ".tools" / "helm" / name.lower()))
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base")
        with apiserver(work / "api") as kubeconfig:
            here = Cluster(repo, kubeconfig)
            assert here.kubectl("apply", "--server-side", "-f", "-", stdin=_crds(repo)).returncode == 0
            assert here.kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=60s").returncode == 0
            code, out = here.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
            assert code == 0, out
            yield here


def test_a_fresh_deploy_leaves_every_app_namespace_in_sync_and_stamped(cluster: Cluster) -> None:
    assert "14 in sync, 0 need attention" in cluster.drift()
    annotations = json.loads(cluster.kubectl("get", "namespace", "ledger-dev", "-o", "jsonpath={.metadata.annotations}").stdout)
    assert len(annotations["berth.contoso.io/deployed-commit"]) == 40
    assert len(annotations["berth.contoso.io/deployed-digest"]) == 64


def test_a_deploy_with_nothing_changed_touches_no_app_namespace(cluster: Cluster) -> None:
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
    assert code == 0, out
    assert "14 unchanged" in out
    assert "applied" not in out.replace("platform: applied", "")


def test_a_variable_added_by_hand_is_reported_and_removed_by_revert(cluster: Cluster) -> None:
    assert cluster.kubectl("set", "env", "deployment/ledger-api", "-n", "ledger-dev", "LOG_LEVEL=debug").returncode == 0
    added = "Deployment/ledger-api: kubectl-set: .spec.template.spec.containers[name=main].env[name=LOG_LEVEL]"
    assert added in cluster.drift()
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "dev")
    assert (code, "1 unchanged" in out) == (0, True)  # git did not change, so the deploy leaves it alone
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "dev", "--revert")
    assert code == 0, out
    assert "removed Deployment/ledger-api" in out
    assert "ledger-dev" not in cluster.drift()


def test_a_scale_by_hand_blocks_deploys_until_marked_break_glass_or_reverted(cluster: Cluster) -> None:
    assert cluster.kubectl("scale", "deployment/payments-api-api", "-n", "payments-api-dev", "--replicas=4").returncode == 0
    report = cluster.drift()
    assert "payments-api-dev: differs from git: Deployment/payments-api-api" in report
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev", "--all")
    assert code == 2  # an app failed; the platform did not
    assert ".spec.replicas" in out
    assert "adopt the change in the spec, or deploy with --revert" in out

    code, out = cluster.berth("breakglass", "payments-api", "--env", "dev", "--reason", "load test needs four replicas")
    assert code == 0, out
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev", "--all")
    assert code == 0, out
    assert "skipped: break-glass by berth-test-admin" in out
    report = cluster.drift()
    assert "break-glass by berth-test-admin until" in report
    assert "ConfigMap/berth-break-glass" not in report  # the mark itself is not drift

    assert cluster.berth("breakglass", "payments-api", "--env", "dev", "--end")[0] == 0
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev", "--revert")
    assert code == 0, out
    assert cluster.kubectl("get", "deployment", "payments-api-api", "-n", "payments-api-dev", "-o", "jsonpath={.spec.replicas}").stdout == "1"
    assert "payments-api-dev" not in cluster.drift()


def test_a_failure_holds_that_app_in_the_next_environment_and_no_other_app(cluster: Cluster, tmp_path: Path) -> None:
    app = cluster.repo / "apps/payments-api/app.yaml"
    original = app.read_text()
    assert cluster.kubectl("scale", "deployment/payments-api-api", "-n", "payments-api-dev", "--replicas=3").returncode == 0
    try:
        app.write_text(original.replace("SETTLEMENT_CURRENCY: USD", "SETTLEMENT_CURRENCY: EUR"))  # reaches dev, qa and prod
        (cluster.repo / "apps/ledger/qa.yaml").write_text((cluster.repo / "apps/ledger/qa.yaml").read_text() + "config:\n  LOG_LEVEL: warn\n")
        cluster.commit("payments-api settles in euros; ledger qa logs warnings")
        results_file = tmp_path / "results.json"
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", "--results-out", str(results_file))
        assert code == 2, out
        results = Results.model_validate_json(results_file.read_text())
        assert results.namespaces["payments-api-dev"].state is State.FAILED
        assert results.namespaces["payments-api-qa"].state is State.HELD
        assert results.namespaces["ledger-qa"].state is State.DEPLOYED  # another app is not held
        assert "payments-api-qa: not deployed" in cluster.drift()

        # The prod cluster's deploy reads qa's results and holds payments-api there too.
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", "--hold-from", str(results_file), "--app", "payments-api", "--env", "qa")
        assert (code, "held" in out) == (2, True)

        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev", "--revert")[0] == 0
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
        assert code == 0, out
        assert "payments-api-qa: applied" in out
        assert "1 deployed" in out  # only qa was left to do
    finally:
        app.write_text(original)
        _git(cluster.repo, "checkout", "-q", "HEAD~1", "--", "apps/ledger/qa.yaml")
        cluster.commit("restore")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0


def test_qa_waits_for_a_version_dev_did_not_run_because_of_break_glass_or_a_deploy_of_qa_alone(cluster: Cluster) -> None:
    app = cluster.repo / "apps/ledger/app.yaml"
    original = app.read_text()
    assert cluster.berth("breakglass", "ledger", "--env", "dev", "--reason", "debugging the ledger in dev")[0] == 0
    try:
        app.write_text(original.replace("LOG_LEVEL: info", "LOG_LEVEL: debug"))  # reaches dev and qa
        cluster.commit("ledger logs at debug")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
        assert code == 2, out
        assert "ledger-dev: skipped: break-glass by berth-test-admin" in out
        assert "ledger-qa: held: ledger was skipped for break-glass in dev, and qa promotes from dev" in out

        assert cluster.berth("breakglass", "ledger", "--env", "dev", "--end")[0] == 0
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa")
        assert code == 2, out
        assert "ledger-qa: held: ledger-dev does not run what git renders for it and is not deployed in this run" in out

        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger")
        assert code == 0, out
        assert "2 deployed" in out
    finally:
        cluster.berth("breakglass", "ledger", "--env", "dev", "--end")
        app.write_text(original)
        cluster.commit("restore")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0


def test_a_change_adopted_into_the_spec_deploys_cleanly_and_stops_being_drift(cluster: Cluster) -> None:
    env_file = cluster.repo / "apps/ledger/qa.yaml"
    original = env_file.read_text()
    assert cluster.kubectl("scale", "deployment/ledger-api", "-n", "ledger-qa", "--replicas=2").returncode == 0
    assert "Deployment/ledger-api: kubectl: .spec.replicas" in cluster.drift()
    try:
        env_file.write_text(original + "services:\n  api: {replicas: 2}\n")
        cluster.commit("keep two ledger replicas in qa")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa")
        assert code == 0, out  # git now sets the same value, so there is no conflict
        assert "ledger-qa" not in cluster.drift()
    finally:
        env_file.write_text(original)
        cluster.commit("restore")
        cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa", "--revert")


def test_a_service_keeps_its_replicas_when_it_starts_autoscaling_and_git_takes_them_back_when_it_stops(cluster: Cluster) -> None:
    env_file = cluster.repo / "apps/ledger/qa.yaml"
    original = env_file.read_text()
    deploy = ("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa")

    def replicas() -> str:
        return cluster.kubectl("get", "deployment", "ledger-api", "-n", "ledger-qa", "-o", "jsonpath={.spec.replicas}").stdout

    try:
        env_file.write_text(original + "services:\n  api: {replicas: 5}\n")
        cluster.commit("five ledger replicas in qa")
        assert cluster.berth(*deploy)[0] == 0
        assert replicas() == "5"

        env_file.write_text(original + "services:\n  api: {autoscale: {min: 3, max: 8}}\n")
        cluster.commit("autoscale ledger in qa")
        code, out = cluster.berth(*deploy)
        assert code == 0, out
        assert "Deployment/ledger-api keeps 5 replicas until its autoscaler takes over" in out
        assert replicas() == "5"  # without the handover, the count git stopped setting would fall back to 1
        assert "ledger-qa" not in cluster.drift()

        # The autoscaler scales through the scale subresource, as kube-controller-manager.
        scale = {"apiVersion": "autoscaling/v1", "kind": "Scale", "metadata": {"name": "ledger-api", "namespace": "ledger-qa"}, "spec": {"replicas": 6}}
        path = "/apis/apps/v1/namespaces/ledger-qa/deployments/ledger-api/scale?fieldManager=kube-controller-manager"
        assert cluster.kubectl("replace", "--raw", path, "-f", "-", stdin=json.dumps(scale)).returncode == 0
        assert replicas() == "6"
        assert "ledger-qa" not in cluster.drift()

        env_file.write_text(original + "services:\n  api: {replicas: 3}\n")
        cluster.commit("three ledger replicas in qa")
        code, out = cluster.berth(*deploy)
        assert code == 0, out  # the autoscaler's count is not a change made by hand, so this is no conflict
        assert "deleted HorizontalPodAutoscaler/ledger-api: git sets the replica count now" in out
        assert replicas() == "3"
        assert cluster.kubectl("get", "hpa", "ledger-api", "-n", "ledger-qa").returncode != 0
        assert "ledger-qa" not in cluster.drift()
    finally:
        env_file.write_text(original)
        cluster.commit("restore")
        assert cluster.berth(*deploy)[0] == 0


def test_an_object_made_by_hand_is_reported(cluster: Cluster) -> None:
    assert cluster.kubectl("create", "configmap", "tmp-fix", "-n", "ledger-qa", "--from-literal=a=b").returncode == 0
    assert "ledger-qa: made by hand: ConfigMap/tmp-fix; not marked" in cluster.drift()
    assert cluster.kubectl("delete", "configmap", "tmp-fix", "-n", "ledger-qa").returncode == 0


def test_an_object_deleted_by_hand_is_reported_missing(cluster: Cluster) -> None:
    assert cluster.kubectl("delete", "networkpolicy", "berth-ingress", "-n", "ledger-qa").returncode == 0
    report = cluster.drift()
    assert "ledger-qa: differs from git: NetworkPolicy/berth-ingress; missing: NetworkPolicy/berth-ingress" in report
    assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa", "--all")[0] == 0
    assert "ledger-qa" not in cluster.drift()


def test_a_deploy_deletes_what_git_dropped_but_never_volumes_unless_told_to_keep_it(cluster: Cluster) -> None:
    batch, indexer = cluster.repo / "apps/risk-batch/app.yaml", cluster.repo / "apps/doc-indexer/app.yaml"
    originals = batch.read_text(), indexer.read_text()
    try:
        cleanup = originals[0][originals[0].index("  cleanup:\n") : originals[0].index("secrets:")]
        batch.write_text(originals[0].replace(cleanup, ""))
        indexer.write_text(originals[1].replace("    volumes:\n      index: {path: /var/lib/indexer, size_gi: 20}\n", ""))
        cluster.commit("no cleanup job; no index volume")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", "--no-prune")
        assert code == 0, out
        assert "risk-batch-dev: kept CronJob/risk-batch-cleanup, which git no longer renders (--no-prune)" in out
        assert "CronJob/risk-batch-cleanup (removed from git)" in cluster.drift()
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", "--all")
        assert code == 0, out
        assert "risk-batch-dev: deleted CronJob/risk-batch-cleanup" in out
        assert cluster.kubectl("get", "cronjob", "risk-batch-cleanup", "-n", "risk-batch-qa").returncode != 0
        assert cluster.kubectl("get", "pvc", "doc-indexer-worker-index", "-n", "doc-indexer-dev").returncode == 0  # data is never pruned
        assert "removed from git" not in cluster.drift()
    finally:
        batch.write_text(originals[0])
        indexer.write_text(originals[1])
        cluster.commit("restore")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0


def test_a_retired_environment_stops_its_workloads_and_a_large_deletion_waits_for_prune_all(cluster: Cluster) -> None:
    platform, retired = cluster.repo / "platform/platform.yaml", cluster.repo / "apps/doc-indexer/qa.yaml"
    originals = platform.read_text(), retired.read_text()
    try:
        # With a tight limit, retiring one app environment is more than a deploy may delete on its own.
        platform.write_text(originals[0] + "deploys:\n  prune_limit: {share: 0.01, floor: 0}\n")
        retired.unlink()
        cluster.commit("doc-indexer leaves qa")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
        assert code == 1, out  # the platform step failed, so nothing was applied
        assert "prune brake: this run would delete" in out
        assert "  Deployment: 1 in 1 namespace" in out
        assert cluster.kubectl("get", "deployment", "doc-indexer-worker", "-n", "doc-indexer-qa").returncode == 0

        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", "--prune-all")
        assert code == 0, out
        assert "doc-indexer-qa: on the cluster but no longer in git; deleted" in out
        assert cluster.kubectl("get", "deployment", "doc-indexer-worker", "-n", "doc-indexer-qa").returncode != 0
        # What a person must decide stays: the Namespace and the volume claim.
        assert cluster.kubectl("get", "namespace", "doc-indexer-qa").returncode == 0
        assert cluster.kubectl("get", "pvc", "doc-indexer-worker-index", "-n", "doc-indexer-qa").returncode == 0
        assert "doc-indexer-qa: on the cluster but no longer in git" in cluster.drift()
    finally:
        platform.write_text(originals[0])
        retired.write_text(originals[1])
        _git(cluster.repo, "add", ".")
        cluster.commit("restore")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0
    assert "doc-indexer-qa" not in cluster.drift()


def _config_maps(cluster: Cluster, namespace: str) -> dict[str, dict[str, str]]:
    found = json.loads(cluster.kubectl("get", "configmaps", "-n", namespace, "-l", "berth.contoso.io/versioned=true", "-o", "json").stdout)
    return {item["metadata"]["name"]: item["metadata"].get("annotations", {}) for item in found["items"]}


def test_settings_that_were_replaced_are_retired_after_a_grace_period(cluster: Cluster) -> None:
    indexer = cluster.repo / "apps/doc-indexer/app.yaml"
    original = indexer.read_text()
    before = set(_config_maps(cluster, "doc-indexer-dev"))
    try:
        indexer.write_text(original.replace("sharepoint = true", "sharepoint = false"))
        cluster.commit("index without SharePoint")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0
        assert len(_config_maps(cluster, "doc-indexer-dev")) == len(before) + 1  # the new one, beside the old
        assert "doc-indexer-dev" not in cluster.drift()  # an old settings ConfigMap is not drift

        # The next deploy sees the Deployment no longer uses the old one and notes when it was superseded...
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0
        (old,) = before
        assert "berth.contoso.io/superseded-at" in _config_maps(cluster, "doc-indexer-dev")[old]
        # ...and a deploy more than a week later deletes it.
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software", now=NOW + timedelta(days=8))
        assert code == 0, out
        assert f"doc-indexer-dev: deleted ConfigMap/{old}" in out
        assert old not in _config_maps(cluster, "doc-indexer-dev")
    finally:
        indexer.write_text(original)
        cluster.commit("restore")
        assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")[0] == 0


def _as_developer(cluster: Cluster, tmp_path: Path) -> Path:
    """A kubeconfig that acts as a developer in dev's break-glass group, by impersonation."""
    text = cluster.kubeconfig.read_text().replace(f"user: {{token: {TOKEN}}}", f"user: {{token: {TOKEN}, as: dana@contoso.com, as-groups: [{DEVELOPERS}, system:authenticated]}}")
    path = tmp_path / "developer.kubeconfig"
    path.write_text(text)
    return path


def test_break_glass_can_change_workloads_but_not_read_secrets_or_open_a_shell(cluster: Cluster, tmp_path: Path) -> None:
    developer = _as_developer(cluster, tmp_path)

    def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN / "kubectl"), f"--kubeconfig={developer}", "--context", "aks-qa", *args], capture_output=True, text=True, check=False)

    def can(verb: str, resource: str, namespace: str = "ledger-dev") -> str:
        return kubectl("auth", "can-i", verb, resource, "-n", namespace).stdout.strip()

    assert can("patch", "deployments") == "yes"
    assert can("create", "jobs") == "yes"
    assert can("get", "secrets") == "no"
    assert can("create", "pods/exec") == "no"
    assert can("create", "rolebindings") == "no"
    assert can("patch", "namespaces/ledger-dev") == "no"
    assert can("patch", "deployments", namespace="kube-system") == "no"
    assert kubectl("scale", "deployment/ledger-api", "-n", "ledger-dev", "--replicas=2").returncode == 0

    code, out = cluster.berth("breakglass", "ledger", "--env", "dev", "--reason", "scaling for an incident", kubeconfig=developer)
    assert code == 0, out
    assert "break-glass by dana@contoso.com" in out
    assert "warning" not in out  # dana is in the group
    assert cluster.berth("breakglass", "ledger", "--env", "dev", "--end", kubeconfig=developer)[0] == 0
    assert cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "dev", "--revert")[0] == 0


def _pod(*, image: str = "contosoacr.azurecr.io/ledger:1@sha256:" + "a" * 64, memory_limit: bool = True) -> dict[str, object]:
    """A pod that passes Pod Security's restricted profile, so only berth's own policies can refuse it."""
    resources: dict[str, object] = {"requests": {"cpu": "100m", "memory": "64Mi"}}
    if memory_limit:
        resources["limits"] = {"memory": "64Mi"}
    container = {"name": "c", "image": image, "resources": resources, "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "probe", "namespace": "ledger-dev"},
        "spec": {"serviceAccountName": "ledger", "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}}, "containers": [container]},
    }


@pytest.mark.parametrize(
    ("doc", "policy"),
    [
        (_pod(image="nginx:latest"), "berth-images-pods"),
        (_pod(memory_limit=False), "berth-resources-pods"),
        (
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ExternalSecret",
                "metadata": {"name": "steal", "namespace": "ledger-dev"},
                "spec": {
                    "secretStoreRef": {"kind": "ClusterSecretStore", "name": "berth-dev"},
                    "target": {"name": "x"},
                    "data": [{"secretKey": "p", "remoteRef": {"key": "payments-api--db-password"}}],  # another app's secret
                },
            },
            "berth-external-secrets",
        ),
        (
            {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "HTTPRoute",
                "metadata": {"name": "steal", "namespace": "customer-portal-dev"},
                "spec": {
                    "parentRefs": [{"name": "berth", "namespace": "envoy-gateway-system", "sectionName": "https-qa"}],
                    "hostnames": ["portal.qa.apps.contoso.com"],  # a qa hostname, from a dev namespace
                    "rules": [{"backendRefs": [{"name": "web", "port": 8080}]}],
                },
            },
            "berth-routes",
        ),
    ],
)
def test_the_api_server_refuses_what_the_renderer_never_produces(cluster: Cluster, doc: dict[str, object], policy: str) -> None:
    refused = cluster.kubectl("create", "--dry-run=server", "-f", "-", stdin=json.dumps(doc))
    assert refused.returncode == 1
    assert f"ValidatingAdmissionPolicy '{policy}'" in refused.stderr


def test_a_namespace_cannot_drop_to_one_owner(cluster: Cluster) -> None:
    refused = cluster.kubectl("annotate", "namespace", "ledger-dev", "berth.contoso.io/app-owners=one@contoso.com", "--overwrite", "--dry-run=server")
    assert "ValidatingAdmissionPolicy 'berth-owners'" in refused.stderr


def test_the_api_server_accepts_a_valid_pod_and_every_pod_the_schedulers_start(cluster: Cluster, tmp_path: Path) -> None:
    assert cluster.kubectl("create", "--dry-run=server", "-f", "-", stdin=json.dumps(_pod())).returncode == 0
    assert cluster.berth("render", "--out", str(tmp_path / "rendered"))[0] == 0
    subprocess.run(["uv", "run", "python", "-I", str(REPO / "scripts/extract_workloads.py"), str(tmp_path / "rendered"), str(tmp_path / "pods")], check=True, capture_output=True)  # noqa: S607
    # This API server stands in for the qa cluster, which runs dev and qa.
    workloads = sorted(path for path in (tmp_path / "pods").glob("*.yaml") if not path.name.startswith(("airflow-prod-", "controlm-prod-")))
    assert workloads
    for workload in workloads:
        done = cluster.kubectl("create", "--dry-run=server", "-f", str(workload))
        assert done.returncode == 0, f"{workload.name}: {done.stderr}"
