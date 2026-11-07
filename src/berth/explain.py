"""One app in one environment as it will run: every setting with the file it came from, the objects berth renders
for it, and where to look at it running. Meant for someone who has just been handed an app."""

from dataclasses import dataclass

from berth.diff import flatten
from berth.fleet import Fleet, Instance
from berth.render import ROOT, instance_docs
from berth.render.airflow import dag_id
from berth.render.k8s import links
from berth.render.pod import telemetry_service

_WIDTH = 60
DEFAULT = "default"


@dataclass(frozen=True)
class Setting:
    path: str
    value: str
    source: str  # the file that set it, or "default"


def _show(path: str, value: object) -> str:
    if path.startswith("files."):
        return f"({len(str(value).splitlines())} lines)"
    text = ", ".join(str(item) for item in value) if isinstance(value, tuple) else str(value)
    if not text:
        return "-"
    return text if len(text) <= _WIDTH else text[: _WIDTH - 3] + "..."


def settings(fleet: Fleet, instance: Instance) -> list[Setting]:
    source = fleet.apps[instance.app]
    app_file, env_file = f"apps/{instance.app}/app.yaml", f"apps/{instance.app}/{instance.env}.yaml"
    # exclude_unset keeps only what someone wrote, so anything else came from a default.
    written_app = flatten(source.spec.model_dump(mode="json", exclude_unset=True))
    written_env = flatten(source.envs[instance.env].model_dump(mode="json", exclude_unset=True))
    resolved = flatten(instance.model_dump(mode="json", exclude={"app", "env"}))
    found: list[Setting] = []
    for path in sorted(resolved):
        if path in written_env and path in written_app:
            origin = f"{env_file} (over {app_file})" if written_env[path] != written_app[path] else env_file
        elif path in written_env:
            origin = env_file
        elif path in written_app:
            origin = app_file
        elif path.endswith(".replicas"):
            origin = f"platform default for {instance.env}"
        else:
            origin = DEFAULT
        found.append(Setting(path, _show(path, resolved[path]), origin))
    found.extend(Setting(f"workflows.{name}", "disabled", env_file) for name, patch in sorted(source.envs[instance.env].workflows.items()) if not patch.enabled)
    return found


def _components(instance: Instance) -> list[str]:
    components = list(instance.services)
    for name, workflow in instance.workflows.items():
        components += [f"{name}-{task}" for task in workflow.tasks]
    return sorted(components)


def text(fleet: Fleet, app: str, env: str) -> str:
    instance = fleet.instance(app, env)
    if instance is None:
        source = fleet.apps.get(app)
        where = "no such app" if source is None else f"it runs in {', '.join(sorted(source.envs))}"
        return f"{app} does not run in {env}: {where}\n"
    platform = fleet.platform
    spec = fleet.apps[app].spec
    cluster = platform.environments[env].cluster
    lines = [
        f"{app} in {env}: {spec.description}",
        f"  owners     {', '.join(spec.owners)}",
        f"  namespace  {instance.namespace} on {cluster}",
        f"  image      {platform.registry}/{spec.repository}:{instance.version}",
        "",
        "Where to look",
        f"  pods and logs   kubectl --context {cluster} -n {instance.namespace} get pods  (or Headlamp, filtered to the namespace)",
        f"  in step?        berth drift --cluster {cluster}",
    ]
    for name, service in sorted(instance.services.items()):
        if service.hostname is not None:
            lines.append(f"  {name} URL  https://{service.hostname}.{platform.environments[env].domain}")
    for component in _components(instance):
        for name, url in links(platform, app, env, component).items():
            lines.append(f"  {name} ({telemetry_service(app, component)})  {url}")
    rows = [(setting.path, setting.value, setting.source) for setting in settings(fleet, instance)]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines += ["", "Settings (merged; the last column is the file that set each one)"]
    lines += [f"  {path.ljust(widths[0])}  {value.ljust(widths[1])}  {origin}" for path, value, origin in rows]
    lines += ["", f"Kubernetes objects ({ROOT}/k8s/{cluster}/{instance.namespace}/manifests.yaml)"]
    for doc in instance_docs(fleet, instance):
        meta = doc.get("metadata")
        name = meta.get("name") if isinstance(meta, dict) else "?"
        lines.append(f"  {doc.get('kind')} {name}")
    scheduled = [(name, workflow) for name, workflow in sorted(instance.workflows.items()) if workflow.orchestrator != "kubernetes"]
    if scheduled:
        lines += ["", "Started by a scheduler"]
        for name, workflow in scheduled:
            if workflow.orchestrator == "airflow":
                lines.append(f"  {name}: Airflow DAG {dag_id(app, name)} ({ROOT}/airflow/{env}/{dag_id(app, name)}.py)")
            else:
                lines.append(f"  {name}: Control-M folder in {ROOT}/controlm/{env}/{app}.json")
    return "\n".join(lines) + "\n"
