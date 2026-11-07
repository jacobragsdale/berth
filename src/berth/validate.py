"""Checks that need more than one file: references between apps, platform menus, and what each orchestrator can express."""

from berth.fleet import Fleet, Instance
from berth.render.controlm import controlm_when


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
