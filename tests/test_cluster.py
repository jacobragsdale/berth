"""`berth deploy`, `drift` and `breakglass` against a real kube-apiserver, and the admission policies it enforces.

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
from datetime import UTC, datetime
from pathlib import Path

import pytest
from apiserver import BIN, apiserver
from support import REPO

from berth import gitio
from berth.cli import parse, run
from berth.fleet import load
from berth.kube import helm_template
from berth.render.software import releases, template_args, values

pytestmark = pytest.mark.integration
NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)


@dataclass(frozen=True)
class Cluster:
    repo: Path
    kubeconfig: Path

    def berth(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = run(parse(["--root", str(self.repo), *argv, *(["--kubeconfig", str(self.kubeconfig)] if argv[0] in {"deploy", "drift", "breakglass"} else [])]), out, NOW)
        return code, out.getvalue()

    def kubectl(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(BIN / "kubectl"), f"--kubeconfig={self.kubeconfig}", "--context", "aks-qa", *args], input=stdin, capture_output=True, text=True, check=False)

    def drift(self) -> str:
        code, out = self.berth("drift", "--cluster", "aks-qa")
        assert code == 0, out
        return out


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
    with pytest.MonkeyPatch.context() as env:
        env.setenv("BERTH_KUBECTL", str(BIN / "kubectl"))
        env.setenv("BERTH_HELM", str(BIN / "helm"))
        for name in ("CACHE", "CONFIG", "DATA"):
            env.setenv(f"HELM_{name}_HOME", str(REPO / ".tools" / "helm" / name.lower()))
        _git(repo, "init", "-q", "-b", "main")
        assert run(parse(["--root", str(repo), "render"]), io.StringIO(), NOW) == 0
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base")
        with apiserver(work / "api") as kubeconfig:
            here = Cluster(repo, kubeconfig)
            assert here.kubectl("apply", "--server-side", "-f", "-", stdin=_crds(repo)).returncode == 0
            assert here.kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=60s").returncode == 0
            code, out = here.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
            assert code == 0, out
            yield here


def test_a_fresh_deploy_leaves_every_app_namespace_in_sync(cluster: Cluster) -> None:
    assert "14 in sync, 0 need attention" in cluster.drift()
    stamped = cluster.kubectl("get", "namespace", "ledger-dev", "-o", "jsonpath={.metadata.annotations.berth\\.contoso\\.io/deployed-commit}")
    assert len(stamped.stdout) == 40


def test_a_variable_added_by_hand_is_reported_kept_by_deploys_and_removed_by_revert(cluster: Cluster) -> None:
    assert cluster.kubectl("set", "env", "deployment/ledger-api", "-n", "ledger-dev", "LOG_LEVEL=debug").returncode == 0
    added = "Deployment/ledger-api: kubectl-set: .spec.template.spec.containers[name=main].env[name=LOG_LEVEL]"
    assert added in cluster.drift()
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "dev")
    assert code == 0, out
    assert "kept a change made by hand" in out
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "dev", "--revert")
    assert code == 0
    assert "removed Deployment/ledger-api" in out
    assert "ledger-dev" not in cluster.drift()


def test_a_scale_by_hand_blocks_deploys_until_marked_break_glass_or_reverted(cluster: Cluster) -> None:
    assert cluster.kubectl("scale", "deployment/payments-api-api", "-n", "payments-api-dev", "--replicas=4").returncode == 0
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev")
    assert code == 1
    assert ".spec.replicas" in out
    assert "adopt the change in the spec, or deploy with --revert" in out

    code, out = cluster.berth("breakglass", "payments-api", "--env", "dev", "--reason", "load test needs four replicas")
    assert code == 0, out
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev")
    assert code == 0, out
    assert "skipped, break-glass by berth-test-admin" in out
    assert "break-glass by berth-test-admin until" in cluster.drift()

    assert cluster.berth("breakglass", "payments-api", "--env", "dev", "--end")[0] == 0
    code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "payments-api", "--env", "dev", "--revert")
    assert code == 0, out
    assert cluster.kubectl("get", "deployment", "payments-api-api", "-n", "payments-api-dev", "-o", "jsonpath={.spec.replicas}").stdout == "1"
    assert "payments-api-dev" not in cluster.drift()


def test_a_change_adopted_into_the_spec_deploys_cleanly_and_stops_being_drift(cluster: Cluster) -> None:
    env_file = cluster.repo / "apps/ledger/qa.yaml"
    original = env_file.read_text()
    assert cluster.kubectl("scale", "deployment/ledger-api", "-n", "ledger-qa", "--replicas=2").returncode == 0
    assert "Deployment/ledger-api: kubectl: .spec.replicas" in cluster.drift()
    try:
        env_file.write_text(original + "services:\n  api: {replicas: 2}\n")
        assert run(parse(["--root", str(cluster.repo), "render"]), io.StringIO(), NOW) == 0
        _git(cluster.repo, "commit", "-qam", "keep two ledger replicas in qa")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa")
        assert code == 0, out  # git now sets the same value, so there is no conflict
        assert "ledger-qa" not in cluster.drift()
    finally:
        env_file.write_text(original)
        assert run(parse(["--root", str(cluster.repo), "render"]), io.StringIO(), NOW) == 0
        _git(cluster.repo, "commit", "-qam", "restore")
        cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "ledger", "--env", "qa", "--revert")


def test_an_object_made_by_hand_is_reported(cluster: Cluster) -> None:
    assert cluster.kubectl("create", "configmap", "tmp-fix", "-n", "ledger-qa", "--from-literal=a=b").returncode == 0
    assert "ledger-qa: made by hand: ConfigMap/tmp-fix; not marked" in cluster.drift()
    assert cluster.kubectl("delete", "configmap", "tmp-fix", "-n", "ledger-qa").returncode == 0


def test_prune_deletes_what_git_dropped_but_never_volumes(cluster: Cluster) -> None:
    app = cluster.repo / "apps/doc-indexer/app.yaml"
    original = app.read_text()
    before = cluster.kubectl("get", "configmaps", "-n", "doc-indexer-dev", "-l", "app.kubernetes.io/managed-by=berth", "-o", "name").stdout.split()
    try:
        # New file contents rename the files ConfigMap, leaving the old one behind; dropping the volume unrenders the claim.
        changed = original.replace("sharepoint = true", "sharepoint = false").replace("    volumes:\n      index: {path: /var/lib/indexer, size_gi: 20}\n", "")
        assert changed != original
        app.write_text(changed)
        assert run(parse(["--root", str(cluster.repo), "render"]), io.StringIO(), NOW) == 0
        _git(cluster.repo, "commit", "-qam", "new files, no volume")
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "doc-indexer", "--env", "dev")
        assert code == 0, out
        assert "is no longer in git; deploy with --prune to delete it" in out
        code, out = cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "doc-indexer", "--env", "dev", "--prune")
        assert code == 0, out
        after = cluster.kubectl("get", "configmaps", "-n", "doc-indexer-dev", "-l", "app.kubernetes.io/managed-by=berth", "-o", "name").stdout.split()
        assert len(after) == len(before)
        assert set(after) != set(before)
        assert cluster.kubectl("get", "pvc", "doc-indexer-worker-index", "-n", "doc-indexer-dev").returncode == 0  # data is never pruned
    finally:
        app.write_text(original)
        assert run(parse(["--root", str(cluster.repo), "render"]), io.StringIO(), NOW) == 0
        _git(cluster.repo, "commit", "-qam", "restore")
        cluster.berth("deploy", "--cluster", "aks-qa", "--no-wait", "--app", "doc-indexer", "--env", "dev", "--prune")


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
    subprocess.run(["uv", "run", "python", "-I", str(REPO / "scripts/extract_workloads.py"), str(cluster.repo / "rendered"), str(tmp_path)], check=True, capture_output=True)  # noqa: S607
    # This API server stands in for the qa cluster, which runs dev and qa.
    workloads = sorted(path for path in tmp_path.glob("*.yaml") if not path.name.startswith(("airflow-prod-", "controlm-prod-")))
    assert workloads
    for workload in workloads:
        done = cluster.kubectl("create", "--dry-run=server", "-f", str(workload))
        assert done.returncode == 0, f"{workload.name}: {done.stderr}"
