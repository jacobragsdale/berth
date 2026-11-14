"""How long berth takes for a few hundred apps, against a real kube-apiserver and etcd.

Its own tier (`pytest -m scale`), slower than the integration tests. The numbers it prints are what docs/design.md
quotes. Nothing runs on the test API server, so rollout waits are not part of them (`--no-wait`).
"""

import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from apiserver import Cluster, started
from support import REPO, app_spec, env_spec

from berth import yamlio

pytestmark = pytest.mark.scale
APPS = 300  # two environments each on the test cluster: 600 app namespaces
ENVS = ("dev", "qa", "prod")


def _app(index: int) -> dict[str, object]:
    """An app shaped like the common case: one service with a company hostname, a nightly job, settings, a secret."""
    name = f"synthetic-{index:03d}"
    return app_spec(
        name=name,
        source=f"https://dev.azure.com/contoso/synthetic/_git/{name}",
        services={"api": {"port": 8080, "health": {"path": "/health"}, "size": "s", "expose": "company", "hostname": name}},
        workflows={"nightly": {"orchestrator": "kubernetes", "schedule": "0 2 * * *", "tasks": {"run": {"size": "xs"}}}},
        config={"LOG_LEVEL": "info", "SHARD": str(index)},
        secrets=["db-password"],
    )


def _repo(root: Path) -> Path:
    repo = root / "repo"
    shutil.copytree(REPO / "platform", repo / "platform")
    shutil.copy(REPO / ".gitignore", repo / ".gitignore")
    for index in range(APPS):
        spec = _app(index)
        folder = repo / "apps" / str(spec["name"])
        folder.mkdir(parents=True)
        (folder / "app.yaml").write_text(yamlio.dump(spec))
        for env in ENVS:
            (folder / f"{env}.yaml").write_text(yamlio.dump(env_spec()))
    return repo


def _timed(label: str, step: Callable[[], tuple[int, str]], timings: dict[str, float]) -> str:
    start = time.perf_counter()
    code, out = step()
    timings[label] = time.perf_counter() - start
    assert code == 0, "\n".join(line for line in out.splitlines() if "applied" not in line)[:4000]
    return out


def test_a_few_hundred_apps_check_render_deploy_and_report_drift_in_minutes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    timings: dict[str, float] = {}
    with started(_repo(tmp_path), tmp_path) as cluster:
        _timed("check", lambda: cluster.berth("check"), timings)
        _timed("render", lambda: cluster.berth("render", "--out", str(tmp_path / "rendered")), timings)
        deploy = ("deploy", "--cluster", "aks-qa", "--no-wait", "--no-software")
        out = _timed("first deploy, --jobs 8", lambda: cluster.berth(*deploy), timings)
        assert f"{APPS * 2} deployed" in out
        for jobs in ("8", "16", "32"):
            _timed(f"deploy --all, --jobs {jobs}", lambda jobs=jobs: cluster.berth(*deploy, "--all", "--jobs", jobs), timings)
        out = _timed("deploy, nothing changed", lambda: cluster.berth(*deploy), timings)
        assert f"{APPS * 2} unchanged" in out
        report = _timed("drift", lambda: _drift(cluster), timings)
        assert f"{APPS * 2} in sync, 0 need attention" in report
        _timed("drift --full", lambda: _drift(cluster, "--full"), timings)
    with capsys.disabled():  # the numbers are the point of the test, so they always reach the terminal
        sys.stdout.write(f"\n{APPS} apps, {APPS * 2} app namespaces on one cluster:\n")
        sys.stdout.writelines(f"  {label}: {seconds:.1f} s\n" for label, seconds in timings.items())


def _drift(cluster: Cluster, *extra: str) -> tuple[int, str]:
    return cluster.berth("drift", "--cluster", "aks-qa", *extra)
