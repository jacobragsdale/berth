"""Boundary models for the files people and pipelines write.

`App` is `apps/<name>/app.yaml`, `EnvFile` is `apps/<name>/<env>.yaml`, and
`Platform` plus `ReviewPolicy` are the platform-owned files under `platform/`.
"""

import re
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False)


_CRON_FIELD = re.compile(r"^(\*|\d+(-\d+)?)(/\d+)?(,(\*|\d+(-\d+)?)(/\d+)?)*$")
_DURATION = re.compile(r"^(?P<n>[1-9]\d*)(?P<unit>[smh])$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600}


def _check_cron(value: str) -> str:
    fields = value.split()
    if len(fields) != 5 or not all(_CRON_FIELD.match(field) for field in fields):  # noqa: PLR2004 - cron has five fields
        msg = f"{value!r} is not a five-field cron expression such as '0 2 * * 1-5'"
        raise ValueError(msg)
    return value


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        msg = f"{value!r} is not an IANA time zone such as 'America/New_York'"
        raise ValueError(msg) from error
    return value


def _check_duration(value: str) -> str:
    if _DURATION.match(value) is None:
        msg = f"{value!r} is not a duration such as '90s', '30m' or '2h'"
        raise ValueError(msg)
    return value


def duration_seconds(value: str) -> int:
    match = _DURATION.match(value)
    if match is None:
        msg = f"{value!r} is not a duration"
        raise ValueError(msg)
    return int(match["n"]) * _DURATION_SECONDS[match["unit"]]


# App names become namespace prefixes (`<app>-<env>`), so they stay well under the 63-character DNS label limit.
AppName = Annotated[str, StringConstraints(pattern=r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")]
PartName = Annotated[str, StringConstraints(pattern=r"^[a-z](?:[a-z0-9-]{0,18}[a-z0-9])?$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]{0,9}$")]
EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]
FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$")]
SecretName = Annotated[str, StringConstraints(pattern=r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")]
Identity = Annotated[str, StringConstraints(pattern=r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")]
ServiceRef = Annotated[str, StringConstraints(pattern=r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?/[a-z](?:[a-z0-9-]{0,18}[a-z0-9])?$")]
# A version is always tag plus digest: the tag is for people, the digest is what runs.
Version = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}@sha256:[0-9a-f]{64}$")]
Cron = Annotated[str, AfterValidator(_check_cron)]
Timezone = Annotated[str, AfterValidator(_check_timezone)]
Duration = Annotated[str, AfterValidator(_check_duration)]
SizeName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{1,8}$")]
Command = tuple[Annotated[str, StringConstraints(min_length=1)], ...]


class Health(Spec):
    path: Annotated[str, StringConstraints(pattern=r"^/\S*$")]


class Autoscale(Spec):
    min: int = Field(ge=1)
    max: int = Field(ge=1)
    cpu_percent: int = Field(default=70, ge=10, le=95)

    @model_validator(mode="after")
    def _min_not_above_max(self) -> Self:
        if self.min > self.max:
            msg = f"autoscale min ({self.min}) is above max ({self.max})"
            raise ValueError(msg)
        return self


class Volume(Spec):
    path: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_./-]+$")]
    size_gi: int = Field(ge=1, le=1024)


def _check_scaling(replicas: int | None, autoscale: Autoscale | None) -> None:
    if replicas is not None and autoscale is not None:
        msg = "set replicas or autoscale, not both"
        raise ValueError(msg)


class Service(Spec):
    command: Command | None = None
    args: Command | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)  # pods run as non-root, so ports below 1024 cannot bind
    health: Health | None = None
    size: SizeName = "s"
    replicas: int | None = Field(default=None, ge=0)
    autoscale: Autoscale | None = None
    expose: Literal["internal", "public"] = "internal"
    hostname: Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")] | None = None  # one label; each environment adds its own domain
    volumes: dict[PartName, Volume] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_scaling(self.replicas, self.autoscale)
        if self.expose == "public" and (self.port is None or self.hostname is None):
            msg = "a public service needs a port and a hostname"
            raise ValueError(msg)
        if self.expose != "public" and self.hostname is not None:
            msg = "hostname is only allowed on a public service"
            raise ValueError(msg)
        if self.health is not None and self.port is None:
            msg = "health checks need a port"
            raise ValueError(msg)
        if self.volumes and (self.autoscale is not None or (self.replicas is not None and self.replicas > 1)):
            msg = "a service with volumes runs one replica (Azure Disk is ReadWriteOnce)"
            raise ValueError(msg)
        return self


class Task(Spec):
    command: Command | None = None
    args: Command | None = None
    size: SizeName = "s"
    timeout: Duration = "1h"
    retries: int = Field(default=0, ge=0, le=5)
    depends_on: tuple[PartName, ...] = ()


class Workflow(Spec):
    orchestrator: Literal["airflow", "controlm", "kubernetes"]
    schedule: Cron | None = None  # None: runs only when triggered
    timezone: Timezone = "UTC"
    tasks: dict[PartName, Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_graph(self) -> Self:
        for name, task in self.tasks.items():
            unknown = sorted(set(task.depends_on) - set(self.tasks))
            if unknown:
                msg = f"task {name!r} depends on unknown task(s) {', '.join(unknown)}"
                raise ValueError(msg)
        _check_acyclic({name: task.depends_on for name, task in self.tasks.items()})
        if self.orchestrator == "kubernetes" and (len(self.tasks) != 1 or self.schedule is None):
            msg = "a kubernetes workflow is a CronJob: exactly one task and a schedule"
            raise ValueError(msg)
        return self


def _check_acyclic(edges: dict[str, tuple[str, ...]]) -> None:
    done: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> None:
        if node in done:
            return
        if node in path:
            cycle = [*path[path.index(node) :], node]
            msg = f"tasks depend on each other in a cycle: {' -> '.join(cycle)}"
            raise ValueError(msg)
        path.append(node)
        for parent in edges[node]:
            visit(parent)
        path.pop()
        done.add(node)

    for node in sorted(edges):
        visit(node)


class OverrideTarget(Spec):
    kind: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Za-z]+$")]
    name: Annotated[str, StringConstraints(min_length=1)]


class Override(Spec):
    """An RFC 7386 JSON merge patch applied to one rendered object: the escape hatch for what the spec cannot say."""

    target: OverrideTarget
    patch: dict[str, object] = Field(min_length=1)
    reason: Annotated[str, StringConstraints(min_length=10)]


class App(Spec):
    name: AppName
    description: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    owners: tuple[Identity, ...] = Field(min_length=2)
    source: Annotated[str, StringConstraints(pattern=r"^https://\S+$")]
    image: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+([._/-][a-z0-9]+)*$")] | None = None
    config: dict[EnvVarName, str] = Field(default_factory=dict)
    files: dict[FileName, str] = Field(default_factory=dict)
    secrets: tuple[SecretName, ...] = ()
    talks_to: tuple[ServiceRef, ...] = ()
    azure_identity: bool = False
    services: dict[PartName, Service] = Field(default_factory=dict)
    workflows: dict[PartName, Workflow] = Field(default_factory=dict)
    overrides: tuple[Override, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for label, values in (("owners", self.owners), ("secrets", self.secrets), ("talks_to", self.talks_to)):
            if len(set(values)) != len(values):
                msg = f"{label} lists the same entry twice"
                raise ValueError(msg)
        if not self.services and not self.workflows:
            msg = "an app needs at least one service or workflow"
            raise ValueError(msg)
        clash = sorted(set(self.services) & set(self.workflows))
        if clash:
            msg = f"service and workflow names must differ: {', '.join(clash)}"
            raise ValueError(msg)
        return self

    @property
    def repository(self) -> str:
        return self.image if self.image is not None else self.name


class ServiceOverlay(Spec):
    size: SizeName | None = None
    replicas: int | None = Field(default=None, ge=0)
    autoscale: Autoscale | None = None

    @model_validator(mode="after")
    def _one_scaling_mode(self) -> Self:
        _check_scaling(self.replicas, self.autoscale)
        return self


class WorkflowOverlay(Spec):
    schedule: Cron | None = None
    enabled: bool = True


class EnvFile(Spec):
    version: Version
    config: dict[EnvVarName, str] = Field(default_factory=dict)
    services: dict[PartName, ServiceOverlay] = Field(default_factory=dict)
    workflows: dict[PartName, WorkflowOverlay] = Field(default_factory=dict)
    overrides: tuple[Override, ...] = ()


class Size(Spec):
    cpu: Annotated[str, StringConstraints(pattern=r"^\d+m?$")]
    memory: Annotated[str, StringConstraints(pattern=r"^\d+(Mi|Gi)$")]
    platform_review: bool = False


class AirflowTarget(Spec):
    namespace: str
    service_account: str


class ControlMTarget(Spec):
    server: str
    connection_profile: str
    timezone: Timezone  # Control-M schedules in its server's zone; workflows on Control-M must use it
    agent_namespace: str
    agent_service_account: str


class Environment(Spec):
    cluster: str
    key_vault: str
    production: bool = False
    promotes_from: EnvName | None = None
    default_replicas: int = Field(ge=1)
    ingress_class: str
    ingress_namespace: str
    public_domain: str
    airflow: AirflowTarget
    controlm: ControlMTarget


Tier = Literal["auto", "owner", "platform"]


class Freeze(Spec):
    """A window in which production changes wait. It delays merges rather than rejecting them; break-glass skips it."""

    start: AwareDatetime
    end: AwareDatetime
    reason: Annotated[str, StringConstraints(min_length=1)]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            msg = "a freeze must end after it starts"
            raise ValueError(msg)
        return self


class Platform(Spec):
    registry: str
    tenant_id: str
    platform_approvers: tuple[Identity, ...] = Field(min_length=1)
    release_bot: Annotated[str, StringConstraints(min_length=1)]
    max_replicas_without_review: int = Field(ge=1)
    storage_class: str
    environments: dict[EnvName, Environment] = Field(min_length=1)
    sizes: dict[SizeName, Size] = Field(min_length=1)
    identities: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+/[a-z0-9]+$")], str] = Field(default_factory=dict)
    freezes: tuple[Freeze, ...] = ()

    @model_validator(mode="after")
    def _promotion_chain_known(self) -> Self:
        for name, env in self.environments.items():
            if env.promotes_from is not None and env.promotes_from not in self.environments:
                msg = f"environment {name!r} promotes from unknown environment {env.promotes_from!r}"
                raise ValueError(msg)
        return self


class Rule(Spec):
    reason: Annotated[str, StringConstraints(min_length=1)]
    paths: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(min_length=1)
    tier: Tier | dict[EnvName, Tier]


class ReviewPolicy(Spec):
    rules: tuple[Rule, ...] = Field(min_length=1)
