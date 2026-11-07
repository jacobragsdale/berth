"""Load a repository tree into validated specs, and resolve each app for each environment it runs in."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from pydantic import ValidationError

from berth import yamlio
from berth.model import App, EnvFile, Environment, Override, Platform, ReviewPolicy, Service, Software, Spec, Workflow

PLATFORM_FILE = "platform/platform.yaml"
POLICY_FILE = "platform/review.yaml"
SOFTWARE_DIR = "platform/software"
APP_FILE = "app.yaml"

type Tree = Mapping[str, str]
"""Repository files by POSIX path relative to the root. Read from a worktree or a git ref, so both load the same way."""


class LoadError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class AppSource:
    spec: App
    envs: Mapping[str, EnvFile]
    other_files: tuple[str, ...]


@dataclass(frozen=True)
class Fleet:
    platform: Platform
    policy: ReviewPolicy
    apps: Mapping[str, AppSource]
    software: Mapping[str, Software]

    def instances(self) -> Iterator["Instance"]:
        for name in sorted(self.apps):
            for env in self.platform.environments:
                instance = self.instance(name, env)
                if instance is not None:
                    yield instance

    def instance(self, app: str, env: str) -> "Instance | None":
        source = self.apps.get(app)
        if source is None or env not in source.envs:
            return None
        return resolve(source.spec, env, self.platform.environments[env], source.envs[env])


class Instance(Spec):
    """One app in one environment, after the environment file is merged over app.yaml. Review diffs compare these."""

    app: str
    env: str
    version: str
    config: dict[str, str]
    files: dict[str, str]
    secrets: tuple[str, ...]
    talks_to: tuple[str, ...]
    azure_identity: bool
    services: dict[str, Service]
    workflows: dict[str, Workflow]
    overrides: tuple[Override, ...]

    @property
    def namespace(self) -> str:
        return f"{self.app}-{self.env}"

    @property
    def tag(self) -> str:
        return self.version.split("@", 1)[0]


def _validated[M: Spec](model: type[M], data: object, path: str, problems: list[str]) -> M | None:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        for item in error.errors():
            location = ".".join(str(part) for part in item["loc"]) or "(top level)"
            problems.append(f"{path}: {location}: {item['msg']}")
        return None


def _parsed[M: Spec](model: type[M], tree: Tree, path: str, problems: list[str]) -> M | None:
    text = tree.get(path)
    if text is None:
        problems.append(f"{path}: missing")
        return None
    try:
        data = yamlio.load(text, source=path)
    except yamlio.YamlError as error:
        problems.append(str(error))
        return None
    return _validated(model, data, path, problems)


def load(tree: Tree) -> Fleet:
    problems: list[str] = []
    platform = _parsed(Platform, tree, PLATFORM_FILE, problems)
    policy = _parsed(ReviewPolicy, tree, POLICY_FILE, problems)
    if platform is None or policy is None:
        raise LoadError(problems)
    _check_policy(policy, platform, problems)

    by_app: dict[str, list[PurePosixPath]] = {}
    for raw in tree:
        path = PurePosixPath(raw)
        if len(path.parts) >= 3 and path.parts[0] == "apps":  # noqa: PLR2004 - apps/<name>/<file>
            by_app.setdefault(path.parts[1], []).append(path)

    apps: dict[str, AppSource] = {}
    for name in sorted(by_app):
        source = _load_app(name, by_app[name], tree, platform, problems)
        if source is not None:
            apps[name] = source
    software = _load_software(tree, platform, problems)
    if problems:
        raise LoadError(problems)
    return Fleet(platform=platform, policy=policy, apps=apps, software=software)


def _directly_in(path: str, directory: str) -> bool:
    parent, _, name = path.rpartition("/")
    return parent == directory and name.endswith(".yaml")


def _load_software(tree: Tree, platform: Platform, problems: list[str]) -> dict[str, Software]:
    found: dict[str, Software] = {}
    releases: dict[tuple[str, str], str] = {}
    for path in sorted(path for path in tree if _directly_in(path, SOFTWARE_DIR)):
        software = _parsed(Software, tree, path, problems)
        if software is None:
            continue
        found[PurePosixPath(path).stem] = software
        for release, install in software.installs.items():
            if install.cluster not in platform.clusters:
                problems.append(f"{path}: installs.{release}: unknown cluster {install.cluster!r} ({', '.join(platform.clusters)})")
            # Objects are labelled with their release, so a release name is unique per cluster.
            other = releases.setdefault((install.cluster, release), path)
            if other != path:
                problems.append(f"{path}: installs.{release}: release name already used on {install.cluster} by {other}")
    return found


def _check_policy(policy: ReviewPolicy, platform: Platform, problems: list[str]) -> None:
    for rule in policy.rules:
        if isinstance(rule.tier, dict):
            missing = sorted(set(platform.environments) - set(rule.tier))
            unknown = sorted(set(rule.tier) - set(platform.environments))
            if missing or unknown:
                problems.append(f"{POLICY_FILE}: rule {rule.reason!r} must give a tier for exactly the environments {', '.join(platform.environments)}")


def _load_app(name: str, paths: list[PurePosixPath], tree: Tree, platform: Platform, problems: list[str]) -> AppSource | None:
    base = f"apps/{name}"
    app = _parsed(App, tree, f"{base}/{APP_FILE}", problems)
    envs: dict[str, EnvFile] = {}
    others: list[str] = []
    for path in sorted(paths):
        if len(path.parts) == 3 and path.suffix == ".yaml" and path.name != APP_FILE:  # noqa: PLR2004 - apps/<name>/<env>.yaml
            if path.stem not in platform.environments:
                problems.append(f"{path}: {path.stem!r} is not an environment ({', '.join(platform.environments)})")
                continue
            env = _parsed(EnvFile, tree, str(path), problems)
            if env is not None:
                envs[path.stem] = env
        elif path.name != APP_FILE:
            others.append(str(path))
    if app is None:
        return None
    if app.name != name:
        problems.append(f"{base}/{APP_FILE}: name {app.name!r} must match the directory name {name!r}")
    if not envs:
        problems.append(f"{base}: no environment file; add at least one of {', '.join(f'{env}.yaml' for env in platform.environments)}")
    for env, overlay in envs.items():
        path = f"{base}/{env}.yaml"
        problems.extend(f"{path}: services.{svc}: no service {svc!r} in app.yaml" for svc in sorted(set(overlay.services) - set(app.services)))
        problems.extend(f"{path}: workflows.{wf}: no workflow {wf!r} in app.yaml" for wf in sorted(set(overlay.workflows) - set(app.workflows)))
        try:
            resolve(app, env, platform.environments[env], overlay)
        except ValidationError as error:
            problems.extend(f"{path}: after merging over app.yaml: {item['msg']}" for item in error.errors())
    return AppSource(spec=app, envs=envs, other_files=tuple(others))


def resolve(app: App, env: str, environment: Environment, overlay: EnvFile) -> Instance:
    services: dict[str, Service] = {}
    for name, service in app.services.items():
        patch = overlay.services.get(name)
        replicas, autoscale, size = service.replicas, service.autoscale, service.size
        if patch is not None:
            if patch.size is not None:
                size = patch.size
            if patch.replicas is not None:
                replicas, autoscale = patch.replicas, None
            if patch.autoscale is not None:
                replicas, autoscale = None, patch.autoscale
        if replicas is None and autoscale is None:
            replicas = 1 if service.volumes else environment.default_replicas
        merged = service.model_dump(exclude={"replicas", "autoscale", "size"}) | {"replicas": replicas, "autoscale": autoscale, "size": size}
        services[name] = Service.model_validate(merged)

    workflows: dict[str, Workflow] = {}
    for name, workflow in app.workflows.items():
        patch = overlay.workflows.get(name)
        if patch is not None and not patch.enabled:
            continue
        schedule = patch.schedule if patch is not None and patch.schedule is not None else workflow.schedule
        workflows[name] = Workflow.model_validate(workflow.model_dump() | {"schedule": schedule})

    return Instance(
        app=app.name,
        env=env,
        version=overlay.version,
        config=app.config | overlay.config,
        files=app.files,
        secrets=app.secrets,
        talks_to=app.talks_to,
        azure_identity=app.azure_identity,
        services=services,
        workflows=workflows,
        overrides=app.overrides + overlay.overrides,
    )
