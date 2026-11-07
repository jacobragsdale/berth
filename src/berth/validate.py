"""Checks that need more than one file: references between apps, platform menus, and what each orchestrator can express."""

from berth.fleet import MIRRORS_FILE, SOFTWARE_DIR, Fleet, Instance
from berth.render.controlm import controlm_when
from berth.render.k8s import gateway_certificates
from berth.render.software import releases, values


def problems(fleet: Fleet) -> list[str]:
    found: list[str] = []
    platform = fleet.platform
    for instance in fleet.instances():
        where = f"apps/{instance.app}/{instance.env}.yaml"
        environment = platform.environments[instance.env]
        sizes = [(f"services.{name}", service.size) for name, service in instance.services.items()]
        sizes += [(f"workflows.{wf}.tasks.{task}", spec.size) for wf, workflow in instance.workflows.items() for task, spec in workflow.tasks.items()]
        found.extend(f"{where}: {path}: unknown size {size!r} (sizes: {', '.join(platform.sizes)})" for path, size in sizes if size not in platform.sizes)
        found.extend(_reference_problems(fleet, instance, where))
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
    return found + _hostname_problems(fleet) + _software_problems(fleet) + _mirror_problems(fleet)


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
