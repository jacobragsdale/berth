"""The rightsizing bot: from Datadog points to a size from the menu, and from a plan to an edited environment file."""

import json
import shutil
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from support import REPO, app_spec, env_spec, fleet

from berth import gitio
from berth.bots_cli import apply, gather
from berth.datadog import Datadog, DatadogSettings, rollup_seconds
from berth.fleet import load
from berth.rightsize import Plan, Recommendation, Usage, Workload, markdown, mebibytes, millicores, percentile, recommend, usage, workloads

FLEET = fleet()
SIZES, SETTINGS = FLEET.platform.sizes, FLEET.platform.rightsizing
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
DAY = 86400.0


def service(size: str = "m", *, autoscaled: bool = False, pods: int = 2) -> Workload:
    return Workload("demo", "prod", ("services", "api"), "api", size, autoscaled=autoscaled, pods=pods)


def seen(cpu: float, memory: float, *, days: float = 14, oom: float = 0) -> Usage:
    return Usage(cpu_millicores=(cpu / 2, cpu), memory_mib=(memory / 2, memory), oom_kills=oom, days=days)


@pytest.mark.parametrize(("values", "fraction", "expected"), [([1, 2, 3, 4], 0.5, 2), ([1, 2, 3, 4], 0.95, 4), ([7], 0.95, 7), (list(range(1, 101)), 0.95, 95)])
def test_percentile_is_nearest_rank(values: list[float], fraction: float, expected: float) -> None:
    assert percentile(values, fraction) == expected


def test_sizes_parse_to_millicores_and_mebibytes() -> None:
    assert (millicores("250m"), millicores("2"), mebibytes("512Mi"), mebibytes("2Gi")) == (250, 2000, 512, 2048)


def test_datadog_points_become_millicores_mebibytes_and_a_span() -> None:
    result = usage([(0, 250_000_000), (DAY * 10, 500_000_000)], [(0, 512 * 1024 * 1024)], [(DAY, 1)])
    assert result == Usage(cpu_millicores=(250.0, 500.0), memory_mib=(512.0,), oom_kills=1, days=10.0)


@pytest.mark.parametrize(
    ("workload", "usage_", "proposed", "reason"),
    [
        (service("m"), seen(100, 200, days=3), None, "only 3.0 days of data"),
        (service("m"), seen(400, 700), None, "the current size fits"),
        (service("m"), seen(150, 300), "s", "smaller size fits"),  # 150m*1.25 and 300Mi*1.3 fit s (250m, 512Mi)
        (service("s"), seen(200, 1200), "l", "larger size fits"),  # 1200Mi*1.3 needs more than m's 1Gi
        (service("m"), seen(50, 300, oom=2), "l", "killed for memory 2 times"),  # an OOM kill always goes up
        (service("m", autoscaled=True), seen(50, 300), None, "the current size fits"),  # CPU kept: m stays
        (service("xxl"), seen(50, 300, oom=1), None, "already the largest size"),
    ],
)
def test_recommendations(workload: Workload, usage_: Usage, proposed: str | None, reason: str) -> None:
    result = recommend(workload, usage_, SIZES, SETTINGS)
    assert result.proposed == proposed
    assert reason in result.reason


def test_the_saving_is_per_pod_and_tasks_have_none() -> None:
    smaller = recommend(service("m", pods=2), seen(150, 300), SIZES, SETTINGS)
    # m (0.5 vCPU, 1 GiB) to s (0.25 vCPU, 0.5 GiB) at $25/vCPU and $3.50/GiB, for two pods.
    assert smaller.monthly_saving == Decimal("16.00")
    task = Workload("demo", "prod", ("workflows", "nightly", "tasks", "load"), "nightly-load", "m", autoscaled=False, pods=0)
    assert recommend(task, seen(150, 300), SIZES, SETTINGS).monthly_saving is None


def test_every_service_and_task_is_a_workload_with_its_datadog_service() -> None:
    spec = app_spec(workflows={"nightly": {"orchestrator": "airflow", "schedule": "0 2 * * *", "tasks": {"load": {"size": "l"}}}})
    found = workloads(fleet({"demo": (spec, {"prod": env_spec()})}), "prod")
    assert [(w.path, w.service, w.size) for w in found] == [(("services", "api"), "demo-api", "s"), (("workflows", "nightly", "tasks", "load"), "demo-nightly-load", "l")]


def test_gather_queries_datadog_for_each_workload_and_recommends() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def transport(url: str, headers: Mapping[str, str]) -> object:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
        calls.append((query, dict(headers)))
        start = NOW.timestamp() - 14 * DAY
        if "cpu" in query:
            values = [[start * 1000, 100_000_000.0], [NOW.timestamp() * 1000, 120_000_000.0]]
        elif "memory" in query:
            values = [[start * 1000, 200.0 * 1024 * 1024], [NOW.timestamp() * 1000, None]]
        else:
            values = []
        return {"status": "ok", "series": [{"pointlist": values}]}

    demo = fleet({"demo": (app_spec(services={"api": {"port": 8080, "size": "m"}}), {"prod": env_spec()})})
    datadog = Datadog(DatadogSettings(dd_api_key=SecretStr("k"), dd_app_key=SecretStr("a")), transport)
    plan = gather(demo, "prod", datadog, now=NOW)
    (item,) = plan.recommendations
    assert (item.current, item.proposed) == ("m", "s")
    cpu_query, headers = calls[0]
    assert cpu_query == f"max:kubernetes.cpu.usage.total{{service:demo-api,env:prod}} by {{pod_name}}.rollup(max, {rollup_seconds(14 * 86400)})"
    assert headers == {"DD-API-KEY": "k", "DD-APPLICATION-KEY": "a"}
    assert calls[2][0].endswith(".rollup(sum, 900)")  # OOM kills are counted, not maxed


def test_a_plan_applies_to_one_app_keeping_comments_and_resolving_per_environment(tmp_path: Path) -> None:
    shutil.copytree(REPO / "platform", tmp_path / "platform")
    shutil.copytree(REPO / "apps", tmp_path / "apps")
    real = load({str(p.relative_to(tmp_path)): p.read_text() for p in tmp_path.rglob("*") if p.is_file()})
    plan = Plan(
        env="qa",
        days=14,
        recommendations=(
            Recommendation(app="risk-batch", env="qa", path=("workflows", "var", "tasks", "compute"), service="risk-batch-var-compute", current="l", proposed="m", reason="r", evidence="e"),
            Recommendation(app="ledger", env="qa", path=("services", "api"), service="ledger-api", current="m", proposed="s", reason="r", evidence="e"),
        ),
    )
    changed = apply(tmp_path, real, plan, "risk-batch")
    assert changed == [tmp_path / "apps/risk-batch/qa.yaml"]
    text = changed[0].read_text()
    assert text.startswith("# yaml-language-server")  # the file's comments survive
    after = load({str(p.relative_to(tmp_path)): p.read_text() for p in tmp_path.rglob("*") if p.is_file()})
    instance = after.instance("risk-batch", "qa")
    assert instance is not None
    assert instance.workflows["var"].tasks["compute"].size == "m"
    prod = after.instance("risk-batch", "prod")
    assert prod is not None
    assert prod.workflows["var"].tasks["compute"].size == "l"  # only qa changed
    assert (tmp_path / "apps/ledger/qa.yaml").read_text() == (REPO / "apps/ledger/qa.yaml").read_text()  # other apps untouched


def test_the_pull_request_says_what_changes_and_why() -> None:
    item = Recommendation(
        app="ledger", env="prod", path=("services", "api"), service="ledger-api", current="m", proposed="s", reason="smaller size fits", evidence="p95 CPU 90m", monthly_saving=Decimal("16.00")
    )
    text = markdown(Plan(env="prod", days=14, recommendations=(item,)), "ledger")
    assert "| `services.api` (`ledger-api`) | m → s | smaller size fits | p95 CPU 90m | $16.00 |" in text
    assert json.loads(Plan(env="prod", days=14, recommendations=(item,)).model_dump_json())["recommendations"][0]["monthly_saving"] == "16.00"


def test_the_real_repository_has_a_rightsizing_bot() -> None:
    assert load(gitio.worktree(REPO)).platform.rightsizing_bot == "berth-rightsizing@contoso.com"
