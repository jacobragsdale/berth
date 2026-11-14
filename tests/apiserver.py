"""A real Kubernetes API server and etcd for integration tests, from the envtest binaries in .tools/bin.

There is no scheduler, kubelet or controller manager: objects are stored, admitted (including berth's
ValidatingAdmissionPolicies and Pod Security) and diffed exactly as AKS would, but nothing runs.
"""

import io
import os
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import REPO

from berth import gitio
from berth.cli import parse, run
from berth.fleet import load
from berth.kube import helm_template
from berth.render.software import releases, template_args, values

BIN = REPO / ".tools" / "bin"
TOKEN = "berth-test-token"  # noqa: S105 - a throwaway token for a throwaway local API server
NOW = datetime(2026, 11, 9, 14, tzinfo=UTC)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _kubeconfig(path: Path, port: int, contexts: tuple[str, ...]) -> None:
    lines = [
        "apiVersion: v1",
        "kind: Config",
        f"clusters: [{{name: local, cluster: {{server: 'https://127.0.0.1:{port}', insecure-skip-tls-verify: true}}}}]",
        f"users: [{{name: admin, user: {{token: {TOKEN}}}}}]",
        "contexts:",
        *(f"  - {{name: {name}, context: {{cluster: local, user: admin}}}}" for name in contexts),
        f"current-context: {contexts[0]}",
    ]
    path.write_text("\n".join(lines) + "\n")


@contextmanager
def apiserver(workdir: Path, contexts: tuple[str, ...] = ("aks-qa",)) -> Generator[Path]:
    """Start etcd and kube-apiserver; yield a kubeconfig whose contexts all point at them."""
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["openssl", "genrsa", "-out", str(workdir / "sa.key"), "2048"], check=True, capture_output=True)  # noqa: S607
    subprocess.run(["openssl", "rsa", "-in", str(workdir / "sa.key"), "-pubout", "-out", str(workdir / "sa.pub")], check=True, capture_output=True)  # noqa: S607
    (workdir / "tokens.csv").write_text(f'{TOKEN},berth-test-admin,1,"system:masters"\n')
    etcd_port, peer_port, api_port = _port(), _port(), _port()
    logs = (workdir / "etcd.log").open("w"), (workdir / "apiserver.log").open("w")
    etcd = subprocess.Popen(
        [
            str(BIN / "etcd"),
            f"--data-dir={workdir / 'etcd'}",
            f"--listen-client-urls=http://127.0.0.1:{etcd_port}",
            f"--advertise-client-urls=http://127.0.0.1:{etcd_port}",
            f"--listen-peer-urls=http://127.0.0.1:{peer_port}",
            "--log-level=error",
        ],
        stdout=logs[0],
        stderr=subprocess.STDOUT,
    )
    server = subprocess.Popen(
        [
            str(BIN / "kube-apiserver"),
            f"--etcd-servers=http://127.0.0.1:{etcd_port}",
            f"--cert-dir={workdir / 'certs'}",
            f"--secure-port={api_port}",
            "--bind-address=127.0.0.1",
            "--service-cluster-ip-range=10.0.0.0/16",  # room for the scale test's Services
            "--service-account-issuer=https://kubernetes.default.svc",
            f"--service-account-key-file={workdir / 'sa.pub'}",
            f"--service-account-signing-key-file={workdir / 'sa.key'}",
            f"--token-auth-file={workdir / 'tokens.csv'}",
            "--authorization-mode=RBAC",
        ],
        stdout=logs[1],
        stderr=subprocess.STDOUT,
    )
    kubeconfig = workdir / "kubeconfig"
    _kubeconfig(kubeconfig, api_port, contexts)
    try:
        deadline = time.monotonic() + 60
        while True:
            ready = subprocess.run([str(BIN / "kubectl"), f"--kubeconfig={kubeconfig}", "get", "--raw=/readyz"], capture_output=True, text=True, check=False)
            if ready.returncode == 0:
                break
            if time.monotonic() > deadline or server.poll() is not None:
                msg = f"kube-apiserver did not become ready; see {workdir / 'apiserver.log'}"
                raise RuntimeError(msg)
            time.sleep(0.5)
        yield kubeconfig
    finally:
        for process in (server, etcd):
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=20)
            if process.poll() is None:
                process.kill()
        for log in logs:
            log.close()


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
        git(self.repo, "commit", "-qam", message)


def git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@contoso.com", *args], check=True, capture_output=True, env=env)  # noqa: S607


def crds(repo: Path) -> str:
    """The CRDs platform software brings (External Secrets, Gateway API), without running its controllers."""
    fleet = load(gitio.worktree(repo))
    docs: list[str] = []
    for release in releases(fleet.software, "aks-qa"):
        rendered = helm_template(str(BIN / "helm"), template_args(release, "1.34.1"), values(release, fleet.platform))
        docs += [doc for doc in rendered.split("\n---") if "kind: CustomResourceDefinition" in doc]
    return "\n---".join(docs)


@contextmanager
def started(repo: Path, work: Path) -> Generator[Cluster]:
    """Commit `repo` as main, start an API server with the platform software's CRDs, and point berth's tools at .tools."""
    if not (BIN / "kube-apiserver").exists():
        pytest.fail("run scripts/fetch-tools.sh first")
    with pytest.MonkeyPatch.context() as env:
        env.setenv("BERTH_KUBECTL", str(BIN / "kubectl"))
        env.setenv("BERTH_HELM", str(BIN / "helm"))
        for name in ("CACHE", "CONFIG", "DATA"):
            env.setenv(f"HELM_{name}_HOME", str(REPO / ".tools" / "helm" / name.lower()))
        git(repo, "init", "-q", "-b", "main")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        with apiserver(work / "api") as kubeconfig:
            here = Cluster(repo, kubeconfig)
            assert here.kubectl("apply", "--server-side", "-f", "-", stdin=crds(repo)).returncode == 0
            assert here.kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=60s").returncode == 0
            yield here
