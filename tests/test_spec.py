"""What the spec refuses (tier 3) and how an environment file merges over app.yaml."""

import pytest
from pydantic import ValidationError
from support import PLATFORM_TEXT, app_spec, env_spec, fleet, tree

from berth import validate, yamlio
from berth.fleet import LoadError, load
from berth.model import App, EnvFile, Platform

GOOD = app_spec()


def test_the_default_spec_is_valid() -> None:
    App.model_validate(GOOD)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"owners": ["ann@contoso.com"]}, "at least 2"),
        ({"owners": ["ann@contoso.com", "ann@contoso.com"]}, "same entry twice"),
        ({"services": {}}, "at least one service or workflow"),
        ({"servics": {}}, "Extra inputs"),  # a typo is an error, not a silently ignored field
        ({"config": {"PORT": 8080}}, "valid string"),  # unquoted YAML numbers do not become strings by accident
        ({"config": {"log-level": "x"}}, "pattern"),
        ({"services": {"web": {"port": 8080, "expose": "company"}}}, "needs a port and a hostname"),
        ({"services": {"web": {"port": 8080, "hostname": "portal"}}}, "only allowed with expose: company"),
        ({"services": {"web": {"port": 80}}}, "greater than or equal to 1024"),
        ({"services": {"w": {"replicas": 2, "volumes": {"d": {"path": "/d", "size_gi": 1}}}}}, "one replica"),
        ({"services": {"w": {"replicas": 2, "autoscale": {"min": 1, "max": 3}}}}, "not both"),
        ({"workflows": {"n": {"orchestrator": "airflow", "schedule": "every night", "tasks": {"t": {}}}}}, "five-field cron"),
        ({"workflows": {"n": {"orchestrator": "airflow", "timezone": "Mars/Base", "tasks": {"t": {}}}}}, "IANA time zone"),
        ({"workflows": {"n": {"orchestrator": "airflow", "tasks": {"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}}}}}, "cycle"),
        ({"workflows": {"n": {"orchestrator": "airflow", "tasks": {"a": {"depends_on": ["zz"]}}}}}, "unknown task"),
        ({"workflows": {"n": {"orchestrator": "kubernetes", "schedule": "0 1 * * *", "tasks": {"a": {}, "b": {}}}}}, "exactly one task"),
        ({"workflows": {"api": {"orchestrator": "airflow", "tasks": {"a": {}}}}}, "names must differ"),
        ({"overrides": [{"target": {"kind": "Deployment", "name": "demo-api"}, "patch": {"x": 1}, "reason": "short"}]}, "at least 10"),
    ],
)
def test_app_spec_rejects(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        App.model_validate(GOOD | change)


@pytest.mark.parametrize("bad", ["1.0.0", "1.0.0@sha256:abc", "latest@sha256:" + "A" * 64])
def test_a_version_needs_a_full_lowercase_digest(bad: str) -> None:
    with pytest.raises(ValidationError):
        EnvFile.model_validate({"version": bad})


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        ({"demo": (app_spec(), {"staging": env_spec()})}, "'staging' is not an environment"),
        ({"demo": (app_spec(), {"dev": env_spec(services={"web": {"replicas": 2}})})}, "no service 'web'"),
        ({"demo": (app_spec(name="other"), {"dev": env_spec()})}, "must match the directory name"),
        ({"demo": (app_spec(), {})}, "no environment file"),
    ],
)
def test_loading_rejects(apps: dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]], message: str) -> None:
    with pytest.raises(LoadError, match=message):
        load(tree(apps))


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        ({"demo": (app_spec(talks_to=["ledger/api"]), {"dev": env_spec()})}, "ledger is not deployed in dev"),
        ({"demo": (app_spec(services={"api": {"port": 8080, "size": "huge"}}), {"dev": env_spec()})}, "unknown size 'huge'"),
        ({"demo": (app_spec(azure_identity=True), {"dev": env_spec()})}, "needs a managed identity"),
        (
            {"demo": (app_spec(workflows={"n": {"orchestrator": "controlm", "schedule": "0 1 * * *", "timezone": "UTC", "tasks": {"t": {}}}}), {"dev": env_spec()})},
            "Control-M schedules in America/New_York",
        ),
        (
            {"demo": (app_spec(workflows={"n": {"orchestrator": "controlm", "schedule": "*/5 * * * *", "timezone": "America/New_York", "tasks": {"t": {}}}}), {"dev": env_spec()})},
            "one fixed time a day",
        ),
        # Names berth joins from the app's own: Kubernetes refuses a label over 63 characters and a CronJob name over 52.
        (
            {"a" * 40: (app_spec(name="a" * 40, workflows={"w" * 12: {"orchestrator": "kubernetes", "schedule": "0 1 * * *", "tasks": {"t": {}}}}), {"dev": env_spec()})},
            "has 53 characters and Kubernetes allows 52",
        ),
        ({"a" * 40: (app_spec(name="a" * 40, workflows={"w" * 10: {"orchestrator": "airflow", "tasks": {"t" * 12: {}}}}), {"dev": env_spec()})}, "has 64 characters and Kubernetes allows 63"),
        # A container's own variables win over its settings, so a setting must not take the name of one berth sets.
        ({"demo": (app_spec(secrets=["db-password"], config={"DB_PASSWORD": "x"}), {"dev": env_spec()})}, "config.DB_PASSWORD: berth sets DB_PASSWORD itself"),
        ({"demo": (app_spec(), {"dev": env_spec(config={"HTTPS_PROXY": "http://elsewhere:1"})})}, "config.HTTPS_PROXY: berth sets HTTPS_PROXY itself"),
        (
            {"demo": (app_spec(talks_to=["ledger/api"], config={"LEDGER_API_URL": "x"}), {"dev": env_spec()}), "ledger": (app_spec(name="ledger"), {"dev": env_spec()})},
            "config.LEDGER_API_URL: berth sets LEDGER_API_URL itself",
        ),
    ],
)
def test_fleet_checks(apps: dict[str, tuple[dict[str, object], dict[str, dict[str, object]]]], message: str) -> None:
    assert any(message in problem for problem in validate.problems(fleet(apps)))


def test_names_at_kubernetes_limits_pass() -> None:
    workflows = {"w" * 11: {"orchestrator": "kubernetes", "schedule": "0 1 * * *", "tasks": {"t" * 10: {}}}}  # 52 and 63 characters
    assert validate.problems(fleet({"a" * 40: (app_spec(name="a" * 40, workflows=workflows), {"dev": env_spec()})})) == []


def test_talks_to_another_deployed_service_is_fine() -> None:
    apps = {"demo": (app_spec(talks_to=["ledger/api"]), {"dev": env_spec()}), "ledger": (app_spec(name="ledger"), {"dev": env_spec()})}
    assert validate.problems(fleet(apps)) == []


def test_environment_file_merges_over_app_yaml() -> None:
    spec = app_spec(
        config={"A": "1", "B": "2"},
        services={"api": {"port": 8080, "autoscale": {"min": 2, "max": 4}}, "worker": {"size": "m"}},
        workflows={"n": {"orchestrator": "airflow", "schedule": "0 1 * * *", "tasks": {"t": {}}}, "m": {"orchestrator": "airflow", "tasks": {"t": {}}}},
    )
    overlay = env_spec(config={"B": "3"}, services={"api": {"replicas": 5}, "worker": {"size": "l"}}, workflows={"n": {"schedule": "0 2 * * *"}, "m": {"enabled": False}})
    instance = fleet({"demo": (spec, {"prod": overlay})}).instance("demo", "prod")
    assert instance is not None
    assert instance.config == {"A": "1", "B": "3"}
    api, worker = instance.services["api"], instance.services["worker"]
    assert (api.replicas, api.autoscale) == (5, None)  # an environment's replicas replace app.yaml's autoscale
    assert (worker.size, worker.replicas) == ("l", 2)  # unset replicas take prod's default
    assert set(instance.workflows) == {"n"}
    assert instance.workflows["n"].schedule == "0 2 * * *"


def test_a_service_with_a_volume_defaults_to_one_replica() -> None:
    spec = app_spec(services={"w": {"volumes": {"d": {"path": "/data", "size_gi": 5}}}})
    instance = fleet({"demo": (spec, {"prod": env_spec()})}).instance("demo", "prod")
    assert instance is not None
    assert instance.services["w"].replicas == 1


@pytest.mark.parametrize(
    ("deploys", "message"),
    [
        ({"field_managers": ["kubectl-edit"]}, "kubectl is how people change things by hand"),
        ({"field_managers": ["aks-addon", "aks-addon"]}, "lists a manager twice"),
        ({"field_managers": ["has space"]}, "String should match pattern"),
        ({"prune_limit": {"share": 0, "floor": 5}}, "greater than 0"),
    ],
)
def test_the_deploy_brakes_and_expected_managers_are_validated(deploys: dict[str, object], message: str) -> None:
    platform = yamlio.load(PLATFORM_TEXT, source="platform.yaml")
    assert isinstance(platform, dict)
    with pytest.raises(ValidationError, match=message):
        Platform.model_validate(platform | {"deploys": deploys})
