"""Boundary models for the files people and pipelines write.

`App` is `apps/<name>/app.yaml`, `EnvFile` is `apps/<name>/<env>.yaml`, and
`Platform` plus `ReviewPolicy` are the platform-owned files under `platform/`.
"""

import re
from decimal import Decimal
from string import Template
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


# A directory a container may write to although the root filesystem is read-only, for images that expect one.
WritablePath = Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_./-]+$")]


class Service(Spec):
    command: Command | None = None
    args: Command | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)  # pods run as non-root, so ports below 1024 cannot bind
    health: Health | None = None
    size: SizeName = "s"
    replicas: int | None = Field(default=None, ge=0)
    autoscale: Autoscale | None = None
    # internal: other apps in the cluster only. company: also callers on the company network, by hostname.
    expose: Literal["internal", "company"] = "internal"
    hostname: Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")] | None = None  # one label; each environment adds its own domain
    volumes: dict[PartName, Volume] = Field(default_factory=dict)
    writable: tuple[WritablePath, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _check_scaling(self.replicas, self.autoscale)
        if self.expose == "company" and (self.port is None or self.hostname is None):
            msg = "a service exposed to the company needs a port and a hostname"
            raise ValueError(msg)
        if self.expose != "company" and self.hostname is not None:
            msg = "hostname is only allowed with expose: company"
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
    writable: tuple[WritablePath, ...] = ()


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
    calls: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$")], ...] = ()  # systems outside the cluster
    azure_identity: bool = False
    services: dict[PartName, Service] = Field(default_factory=dict)
    workflows: dict[PartName, Workflow] = Field(default_factory=dict)
    overrides: tuple[Override, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for label, values in (("owners", self.owners), ("secrets", self.secrets), ("talks_to", self.talks_to), ("calls", self.calls)):
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


class TaskOverlay(Spec):
    size: SizeName | None = None


class WorkflowOverlay(Spec):
    schedule: Cron | None = None
    enabled: bool = True
    tasks: dict[PartName, TaskOverlay] = Field(default_factory=dict)


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


ClusterName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://[^\s/]+(/\S*)?$")]
DnsLabel = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
Domain = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")]


class Proxy(Spec):
    """The corporate proxy. Rendered into every container in upper and lower case, because tools differ."""

    url: Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
    # Never proxied, in addition to the cluster's own names and addresses, which berth always adds.
    no_proxy: tuple[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.*/:-]+$")], ...] = ()


class Environment(Spec):
    cluster: ClusterName
    key_vault: str
    production: bool = False
    promotes_from: EnvName | None = None
    default_replicas: int = Field(ge=1)
    domain: Domain  # company hostnames are <hostname>.<domain>; a wildcard DNS record points it at the cluster's Gateway
    proxy: Proxy | None = None
    # Where pods send OpenTelemetry. `$(HOST_IP)` is the node's address, for an agent running as a DaemonSet.
    otlp_endpoint: str | None = None
    airflow: AirflowTarget
    controlm: ControlMTarget


IPv4 = Annotated[str, StringConstraints(pattern=r"^(\d{1,3}\.){3}\d{1,3}$")]
Cidr = Annotated[str, StringConstraints(pattern=r"^(\d{1,3}\.){3}\d{1,3}/\d{1,2}$")]
EntraObjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class Cluster(Spec):
    key_vault: str  # platform secrets on this cluster, such as the Gateway's certificates
    gateway_ip: IPv4  # the reserved private address of the cluster's Gateway, where the wildcard DNS records point
    networks: tuple[Cidr, ...] = Field(min_length=1)  # pod and service ranges, never sent to the proxy


class Gateway(Spec):
    """The Gateway API implementation the platform installs. Each cluster gets one Gateway named `berth`."""

    namespace: DnsLabel  # where the Gateway lives and its proxies run; app network policies admit traffic from it
    controller: Annotated[str, StringConstraints(min_length=1)]


LINK_FIELDS = frozenset({"app", "env", "namespace", "service", "cluster"})
_LINK_FIELD = re.compile(r"\{([^{}]*)\}")


def _check_link(value: str) -> str:
    unknown = sorted(set(_LINK_FIELD.findall(value)) - LINK_FIELDS)
    if unknown:
        msg = f"unknown placeholder(s) {', '.join(unknown)}; use {', '.join(sorted(LINK_FIELDS))}"
        raise ValueError(msg)
    return value


LinkTemplate = Annotated[HttpsUrl, AfterValidator(_check_link)]


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


def _check_query(value: str) -> str:
    missing = sorted({"service", "env"} - {m[1] or m[2] for m in Template.pattern.finditer(value) if m[1] or m[2]})
    if missing:
        msg = f"a Datadog query needs ${{service}} and ${{env}}; missing {', '.join(missing)}"
        raise ValueError(msg)
    return value


DatadogQuery = Annotated[str, AfterValidator(_check_query)]
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


class Rightsizing(Spec):
    """How the weekly rightsizing bot reads Datadog and turns usage into a size from the menu.

    Queries are Datadog metric queries with `$service` and `$env`, grouped by pod. Check the metric names against
    your account: they differ between Datadog's Kubernetes integrations.
    """

    days: int = Field(default=14, ge=7, le=60)  # how far back to look
    min_days: int = Field(default=7, ge=1)  # less data than this and the bot leaves the size alone
    cpu_percentile: float = Field(default=0.95, gt=0, le=1)  # CPU is forgiving: a high percentile, not the peak
    cpu_margin: float = Field(default=1.25, ge=1)
    memory_margin: float = Field(default=1.3, ge=1)  # memory is not: the peak, plus headroom
    cpu_query: DatadogQuery = "max:kubernetes.cpu.usage.total{service:$service,env:$env} by {pod_name}"  # nanocores
    memory_query: DatadogQuery = "max:kubernetes.memory.working_set{service:$service,env:$env} by {pod_name}"  # bytes
    oom_query: DatadogQuery = "sum:kubernetes_state.container.status_report.count.terminated{service:$service,env:$env,reason:oomkilled}"
    price_per_vcpu_month: Money = Decimal("25.00")  # for the savings estimate in pull requests only
    price_per_gib_month: Money = Decimal("3.50")

    @model_validator(mode="after")
    def _enough_days(self) -> Self:
        if self.min_days > self.days:
            msg = "min_days cannot be more than days"
            raise ValueError(msg)
        return self


class Platform(Spec):
    registry: str
    tenant_id: str
    repo_url: HttpsUrl  # this repository, for links to commits
    platform_approvers: tuple[Identity, ...] = Field(min_length=1)
    release_bot: Annotated[str, StringConstraints(min_length=1)]
    # Identity whose pull requests may change sizes only: the weekly rightsizing bot.
    rightsizing_bot: Annotated[str, StringConstraints(min_length=1)] | None = None
    rightsizing: Rightsizing = Field(default_factory=Rightsizing)
    # Entra groups whose members may change app namespaces with kubectl, for break-glass. They get `edit` there.
    kubectl_writers: tuple[EntraObjectId, ...] = ()
    max_replicas_without_review: int = Field(ge=1)
    storage_class: str
    clusters: dict[ClusterName, Cluster] = Field(min_length=1)
    gateway: Gateway
    environments: dict[EnvName, Environment] = Field(min_length=1)
    sizes: dict[SizeName, Size] = Field(min_length=1)
    identities: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+/[a-z0-9]+$")], str] = Field(default_factory=dict)
    # Links shown by `berth explain` and in the drift report, e.g. to the service in Datadog.
    links: dict[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,30}$")], LinkTemplate] = Field(default_factory=dict)
    freezes: tuple[Freeze, ...] = ()

    @model_validator(mode="after")
    def _references_known(self) -> Self:
        for name, env in self.environments.items():
            if env.promotes_from is not None and env.promotes_from not in self.environments:
                msg = f"environment {name!r} promotes from unknown environment {env.promotes_from!r}"
                raise ValueError(msg)
            if env.cluster not in self.clusters:
                msg = f"environment {name!r} is on unknown cluster {env.cluster!r}"
                raise ValueError(msg)
        return self

    def envs_on(self, cluster: str) -> list[str]:
        return [name for name, env in self.environments.items() if env.cluster == cluster]


class SoftwareSecret(Spec):
    """A Kubernetes Secret platform software reads, filled from the cluster's platform Key Vault by External Secrets."""

    type: Literal["Opaque", "kubernetes.io/tls"] = "Opaque"
    data: dict[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]+$")], Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,127}$")]] = Field(min_length=1)


ChartVersion = Annotated[str, StringConstraints(pattern=r"^v?\d+\.\d+\.\d+$")]  # exact: a range would change what runs without a commit


class SoftwareInstall(Spec):
    cluster: ClusterName
    namespace: DnsLabel
    version: ChartVersion | None = None  # overrides the shared version, to upgrade qa before prod
    values: dict[str, object] = Field(default_factory=dict)  # merged over the shared values (RFC 7386)
    secrets: dict[SecretName, SoftwareSecret] = Field(default_factory=dict)


class Software(Spec):
    """Cluster software (controllers, CRDs, webhooks) as a pinned Helm chart: `platform/software/<name>.yaml`.

    Anything that only runs a container, such as Redis, is an ordinary app instead. Charts are rendered with
    `helm template` at deploy time and applied by berth like everything else; Helm itself never installs anything.
    """

    description: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    repo: Annotated[str, StringConstraints(pattern=r"^(https|oci)://\S+$")]
    chart: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
    version: ChartVersion
    order: int = Field(default=0, ge=0, le=99)  # lower deploys first, e.g. CRDs other software needs
    values: dict[str, object] = Field(default_factory=dict)
    installs: dict[DnsLabel, SoftwareInstall] = Field(min_length=1)  # by Helm release name


class Rule(Spec):
    reason: Annotated[str, StringConstraints(min_length=1)]
    paths: tuple[Annotated[str, StringConstraints(min_length=1)], ...] = Field(min_length=1)
    tier: Tier | dict[EnvName, Tier]


class ReviewPolicy(Spec):
    rules: tuple[Rule, ...] = Field(min_length=1)


class Mirror(Spec):
    """An upstream image kept current in the registry, for third-party apps (`image: <target>`)."""

    source: Annotated[str, StringConstraints(pattern=r"^[a-z0-9.-]+(:\d+)?(/[a-z0-9._-]+)+$")]  # e.g. docker.io/library/redis
    target: Annotated[str, StringConstraints(pattern=r"^mirror(/[a-z0-9._-]+)+$")]  # the repository in the registry
    # Which releases to follow: "8.2" means the newest 8.2.x, "8" the newest 8.x. Tags are plain X.Y.Z plus suffix.
    track: Annotated[str, StringConstraints(pattern=r"^\d+(\.\d+)?$")]
    suffix: Annotated[str, StringConstraints(pattern=r"^(-[a-z0-9.]+)?$")] = ""  # a tag variant such as "-alpine"


class Mirrors(Spec):
    mirrors: dict[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,40}$")], Mirror] = Field(default_factory=dict)
