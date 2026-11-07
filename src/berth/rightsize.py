"""Turn observed CPU and memory into a size from the platform's menu. Pure: `berth.bots_cli` fetches the usage.

The rules, in order:
1. Too little history: leave the size alone.
2. Killed for running out of memory: one size up, whatever the averages say.
3. Otherwise the smallest size, in menu order, that holds a high percentile of CPU and the peak of memory, each
   with a margin. CPU is forgiving (a busy pod is slowed, and berth sets no CPU limit), memory is not (a pod over its
   limit is killed), hence a percentile for one and the peak for the other.
4. A service that autoscales on CPU keeps its CPU request, because the autoscaler's target is a percentage of it.
"""

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from berth.fleet import Fleet
from berth.model import Rightsizing, Size
from berth.render.pod import telemetry_service

MIB_PER_GIB = 1024
_SECONDS_PER_DAY = 86400


def millicores(cpu: str) -> int:
    return int(cpu[:-1]) if cpu.endswith("m") else int(cpu) * 1000


def mebibytes(memory: str) -> int:
    return int(memory[:-2]) * MIB_PER_GIB if memory.endswith("Gi") else int(memory[:-2])


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value at or above that share of the observations."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


@dataclass(frozen=True)
class Workload:
    """One thing that has a size: a service, or a task of a workflow, in one environment."""

    app: str
    env: str
    path: tuple[str, ...]  # where `size` lives in the environment file: ("services", name) or ("workflows", wf, "tasks", task)
    component: str  # its Datadog service is <app>-<component>
    size: str
    autoscaled: bool
    pods: int  # for the savings estimate; 0 for tasks, which run part of the time

    @property
    def service(self) -> str:
        return telemetry_service(self.app, self.component)

    @property
    def label(self) -> str:
        return ".".join(self.path)


def workloads(fleet: Fleet, env: str) -> list[Workload]:
    found: list[Workload] = []
    for instance in fleet.instances():
        if instance.env != env:
            continue
        for name, service in sorted(instance.services.items()):
            pods = service.autoscale.min if service.autoscale is not None else (service.replicas or 0)
            found.append(Workload(instance.app, env, ("services", name), name, service.size, autoscaled=service.autoscale is not None, pods=pods))
        for workflow_name, workflow in sorted(instance.workflows.items()):
            for task_name, task in sorted(workflow.tasks.items()):
                found.append(Workload(instance.app, env, ("workflows", workflow_name, "tasks", task_name), f"{workflow_name}-{task_name}", task.size, autoscaled=False, pods=0))
    return found


@dataclass(frozen=True)
class Usage:
    cpu_millicores: tuple[float, ...]  # per pod, per rollup interval
    memory_mib: tuple[float, ...]
    oom_kills: float
    days: float  # how much history the samples span


def usage(cpu: Iterable[tuple[float, float]], memory: Iterable[tuple[float, float]], oom: Iterable[tuple[float, float]]) -> Usage:
    """From Datadog points: CPU in nanocores, memory in bytes, OOM kills as counts."""
    cpu_points, memory_points = list(cpu), list(memory)
    stamps = [stamp for stamp, _ in cpu_points + memory_points]
    days = (max(stamps) - min(stamps)) / _SECONDS_PER_DAY if stamps else 0.0
    return Usage(
        cpu_millicores=tuple(value / 1_000_000 for _, value in cpu_points), memory_mib=tuple(value / (1024 * 1024) for _, value in memory_points), oom_kills=sum(value for _, value in oom), days=days
    )


class Recommendation(BaseModel):
    """One line of the plan the pipeline applies. Written as JSON between the query and the pull requests."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
    app: str
    env: str
    path: tuple[str, ...]
    service: str
    current: str
    proposed: str | None  # None: leave it alone
    reason: str
    evidence: str
    monthly_saving: Decimal | None = None  # negative when the proposal costs more

    @property
    def changes(self) -> bool:
        return self.proposed is not None and self.proposed != self.current


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)
    env: str
    days: int
    recommendations: tuple[Recommendation, ...] = Field(default=())

    def apps(self) -> list[str]:
        return sorted({item.app for item in self.recommendations if item.changes})


def _cost(size: Size, settings: Rightsizing) -> Decimal:
    return Decimal(millicores(size.cpu)) / 1000 * settings.price_per_vcpu_month + Decimal(mebibytes(size.memory)) / MIB_PER_GIB * settings.price_per_gib_month


def recommend(workload: Workload, seen: Usage, sizes: Mapping[str, Size], settings: Rightsizing) -> Recommendation:
    menu = list(sizes)
    current = sizes[workload.size]

    def result(proposed: str | None, reason: str, evidence: str = "") -> Recommendation:
        saving = None
        if proposed is not None and proposed != workload.size and workload.pods:
            saving = ((_cost(current, settings) - _cost(sizes[proposed], settings)) * workload.pods).quantize(Decimal("0.01"))
        path = workload.path
        return Recommendation(
            app=workload.app, env=workload.env, path=path, service=workload.service, current=workload.size, proposed=proposed, reason=reason, evidence=evidence, monthly_saving=saving
        )

    if not seen.cpu_millicores or not seen.memory_mib or seen.days < settings.min_days:
        return result(None, f"only {seen.days:.1f} days of data; needs {settings.min_days}")
    cpu = percentile(seen.cpu_millicores, settings.cpu_percentile)
    memory = max(seen.memory_mib)
    evidence = f"p{round(settings.cpu_percentile * 100)} CPU {cpu:.0f}m, peak memory {memory:.0f}Mi, over {seen.days:.0f} days"
    if seen.oom_kills > 0:
        bigger = [name for name in menu[menu.index(workload.size) + 1 :] if mebibytes(sizes[name].memory) > mebibytes(current.memory)]
        if not bigger:
            return result(None, f"killed for memory {seen.oom_kills:.0f} times, already the largest size", evidence)
        return result(bigger[0], f"killed for memory {seen.oom_kills:.0f} times", evidence)
    need_cpu = millicores(current.cpu) if workload.autoscaled else cpu * settings.cpu_margin
    need_memory = memory * settings.memory_margin
    fits = [name for name in menu if millicores(sizes[name].cpu) >= need_cpu and mebibytes(sizes[name].memory) >= need_memory]
    proposed = fits[0] if fits else menu[-1]
    if proposed == workload.size:
        return result(None, "the current size fits", evidence)
    direction = "smaller" if menu.index(proposed) < menu.index(workload.size) else "larger"
    note = "; CPU kept for the autoscaler" if workload.autoscaled else ""
    return result(proposed, f"{direction} size fits the observed use with margin{note}", evidence)


def markdown(plan: Plan, app: str) -> str:
    """The pull request description for one app: what changes, why, and the numbers behind it."""
    items = [item for item in plan.recommendations if item.app == app and item.changes]
    lines = [f"Rightsizing for **{app}** in **{plan.env}**, from Datadog over the last {plan.days} days.", "", "| What | Size | Why | Observed | Monthly saving |", "|---|---|---|---|---|"]
    for item in items:
        saving = "-" if item.monthly_saving is None else f"${item.monthly_saving}"
        lines.append(f"| `{'.'.join(item.path)}` (`{item.service}`) | {item.current} → {item.proposed} | {item.reason} | {item.evidence} | {saving} |")
    lines += ["", "Close the pull request to keep the current sizes. The bot proposes again next week if the numbers still say so."]
    return "\n".join(lines) + "\n"


def text(plan: Plan) -> str:
    lines = [f"rightsizing {plan.env}, last {plan.days} days:"]
    for item in plan.recommendations:
        change = f"{item.current} -> {item.proposed}" if item.changes else f"{item.current} (keep)"
        lines.append(f"  {item.app} {'.'.join(item.path)}: {change}: {item.reason}{'; ' + item.evidence if item.evidence else ''}")
    return "\n".join(lines) + "\n"
