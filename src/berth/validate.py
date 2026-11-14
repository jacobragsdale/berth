"""Checks that need more than one file: references between apps, platform menus, and what each orchestrator can express."""

from collections.abc import Mapping

from berth import yamlio
from berth.fleet import MIRRORS_FILE, SOFTWARE_DIR, Fleet, Instance
from berth.model import Platform
from berth.render import context
from berth.render.controlm import controlm_when
from berth.render.k8s import gateway_certificates
from berth.render.pod import berth_variables, telemetry_service
from berth.render.software import releases, values

# Kubernetes' own limits on names berth builds from an app's, which no schema checks: a label value, and a CronJob's
# name, which is shorter because each Job it starts adds a suffix.
_LABEL_VALUE = 63
_CRONJOB_NAME = 52


def problems(fleet: Fleet) -> list[str]:
    found: list[str] = []
    platform = fleet.platform
    for instance in fleet.instances():
        where = f"apps/{instance.app}/{instance.env}.yaml"
        environment = platform.environments[instance.env]
        sizes = [(f"services.{name}", service.size) for name, service in instance.services.items()]
        sizes += [(f"workflows.{wf}.tasks.{task}", spec.size) for wf, workflow in instance.workflows.items() for task, spec in workflow.tasks.items()]
        found.extend(f"{where}: {path}: unknown size {size!r} (sizes: {', '.join(platform.sizes)})" for path, size in sizes if size not in platform.sizes)
        references = _reference_problems(fleet, instance, where)
        found.extend(references)
        if not references:  # the variables include one set per service called, so those must exist first
            taken = berth_variables(context(fleet, instance))
            found.extend(f"{where}: config.{key}: berth sets {key} itself, and its value would win; rename the setting" for key in sorted(taken & set(instance.config)))
        if instance.azure_identity and f"{instance.app}/{instance.env}" not in platform.identities:
            found.append(f"{where}: azure_identity needs a managed identity for {instance.app}/{instance.env} in platform/platform.yaml (ask the platform team)")
        for name, workflow in instance.workflows.items():
            if workflow.orchestrator != "controlm":
                continue
            if workflow.timezone != environment.controlm.timezone:
                found.append(f"{where}: workflows.{name}: Control-M schedules in {environment.controlm.timezone}; set timezone to that or use airflow")
            if workflow.schedule is not None:
                try:
                    controlm_when(workflow.schedule)
                except ValueError as error:
                    found.append(f"{where}: workflows.{name}: {error}")
    return found + _name_problems(fleet) + _hostname_problems(fleet) + _software_problems(fleet) + _mirror_problems(fleet)


def _name_problems(fleet: Fleet) -> list[str]:
    """Names berth joins from an app's own must still fit Kubernetes' limits, or the API server refuses the deploy."""
    found: list[str] = []
    for app, source in sorted(fleet.apps.items()):
        for name, workflow in sorted(source.spec.workflows.items()):
            where = f"apps/{app}/app.yaml: workflows.{name}"
            cronjob = f"{app}-{name}"
            if workflow.orchestrator == "kubernetes" and len(cronjob) > _CRONJOB_NAME:
                found.append(f"{where}: the CronJob name {cronjob!r} has {len(cronjob)} characters and Kubernetes allows {_CRONJOB_NAME}; shorten the workflow's name")
            for task in sorted(workflow.tasks):
                service = telemetry_service(app, f"{name}-{task}")
                if len(service) > _LABEL_VALUE:
                    found.append(f"{where}.tasks.{task}: its label {service!r} has {len(service)} characters and Kubernetes allows {_LABEL_VALUE}; shorten the workflow's or the task's name")
    return found


PIPELINE_TARGETS = "pipelines/templates/targets.yml"


def pipeline_problems(platform: Platform, tree: Mapping[str, str]) -> list[str]:
    """Azure Pipelines loops over clusters and environments when it compiles a pipeline, before berth runs, so the
    pipelines keep their own list in one template. It must say what platform.yaml says, in promotion order."""
    text = tree.get(PIPELINE_TARGETS)
    if text is None:
        return []
    try:
        data = yamlio.load(text, source=PIPELINE_TARGETS)
    except yamlio.YamlError as error:
        return [str(error)]
    variables = data.get("variables") if isinstance(data, dict) else None
    if not isinstance(variables, dict):
        return [f"{PIPELINE_TARGETS}: expected a mapping under variables"]
    order = platform.promotion_order()
    expected = {"clusters": ",".join(dict.fromkeys(platform.environments[env].cluster for env in order)), "environments": ",".join(order), "registry": platform.registry.split(".", 1)[0]}
    return [f"{PIPELINE_TARGETS}: {name} is {variables.get(name)!r}, but platform/platform.yaml says {value!r}" for name, value in expected.items() if variables.get(name) != value]


def _mirror_problems(fleet: Fleet) -> list[str]:
    """Every third-party image has a mirror, so something keeps it current; no two mirrors fill one target."""
    targets: dict[str, str] = {}
    found: list[str] = []
    for name, mirror in sorted(fleet.mirrors.mirrors.items()):
        other = targets.setdefault(mirror.target, name)
        if other != name:
            found.append(f"{MIRRORS_FILE}: mirrors.{name}: target {mirror.target} is already filled by {other}")
    found.extend(
        f"apps/{name}/app.yaml: image {source.spec.image} has no entry in {MIRRORS_FILE}, so nothing keeps it current"
        for name, source in sorted(fleet.apps.items())
        if source.spec.image is not None and source.spec.image.startswith("mirror/") and source.spec.image not in targets
    )
    return found


def _hostname_problems(fleet: Fleet) -> list[str]:
    """Two services may not answer on the same company hostname in one environment."""
    claimed: dict[tuple[str, str], str] = {}
    found: list[str] = []
    for instance in fleet.instances():
        for name, service in sorted(instance.services.items()):
            if service.hostname is None:
                continue
            where = f"{instance.app}/{name}"
            other = claimed.setdefault((instance.env, service.hostname), where)
            if other != where:
                found.append(f"apps/{instance.app}/app.yaml: services.{name}.hostname: {service.hostname!r} is already used in {instance.env} by {other}")
    return found


def _software_problems(fleet: Fleet) -> list[str]:
    found: list[str] = []
    platform = fleet.platform
    app_namespaces = {(platform.environments[instance.env].cluster, instance.namespace) for instance in fleet.instances()}
    for cluster in sorted(platform.clusters):
        installed = releases(fleet.software, cluster)
        for release in installed:
            where = f"{SOFTWARE_DIR}/{release.software}.yaml: installs.{release.name}"
            if (cluster, release.install.namespace) in app_namespaces:
                found.append(f"{where}: {release.install.namespace!r} is already an app namespace on {cluster}")
            try:
                values(release, platform)
            except ValueError as error:
                found.append(f"{where}: values: {error}")
        if not installed:
            continue
        # The Gateway needs a certificate per environment, declared as a software secret in the gateway namespace.
        declared = {name for release in installed if release.install.namespace == platform.gateway.namespace for name in release.install.secrets}
        missing = [name for name in gateway_certificates(platform, cluster) if name not in declared]
        found.extend(f"{SOFTWARE_DIR}/: {cluster}: no install in {platform.gateway.namespace} declares the Gateway certificate {name!r}" for name in missing)
    return found


def _reference_problems(fleet: Fleet, instance: Instance, where: str) -> list[str]:
    found: list[str] = []
    for ref in instance.talks_to:
        app, service = ref.split("/", 1)
        if app == instance.app:
            found.append(f"{where}: talks_to {ref}: an app reaches its own services without declaring them")
            continue
        target = fleet.instance(app, instance.env)
        if target is None:
            found.append(f"{where}: talks_to {ref}: {app} is not deployed in {instance.env}")
            continue
        spec = target.services.get(service)
        if spec is None or spec.port is None:
            found.append(f"{where}: talks_to {ref}: {app} has no service {service!r} with a port")
    return found
