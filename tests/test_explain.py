"""`berth explain`: each merged setting must name the file that actually set it."""

from support import app_spec, env_spec, fleet

from berth.explain import Setting, settings, text


def _origins(**envs: dict[str, object]) -> dict[str, str]:
    spec = app_spec(config={"LOG_LEVEL": "info", "REGION": "eastus"}, workflows={"nightly": {"orchestrator": "airflow", "schedule": "0 2 * * *", "tasks": {"run": {}}}})
    demo = fleet({"demo": (spec, {env: env_spec(**body) for env, body in envs.items()})})
    instance = demo.instance("demo", next(iter(envs)))
    assert instance is not None
    return {setting.path: setting.source for setting in settings(demo, instance)}


def test_values_are_traced_to_app_yaml_the_environment_file_or_a_default() -> None:
    origins = _origins(prod={"config": {"LOG_LEVEL": "warn"}, "services": {"api": {"autoscale": {"min": 2, "max": 4}}}})
    assert origins["config.LOG_LEVEL"] == "apps/demo/prod.yaml (over apps/demo/app.yaml)"
    assert origins["config.REGION"] == "apps/demo/app.yaml"
    assert origins["services.api.autoscale.min"] == "apps/demo/prod.yaml"
    assert origins["services.api.autoscale.cpu_percent"] == "default"
    assert _origins(qa={})["services.api.replicas"] == "platform default for qa"
    assert origins["services.api.expose"] == "default"
    assert origins["version"] == "apps/demo/prod.yaml"


def test_a_workflow_disabled_in_the_environment_file_is_listed_as_disabled() -> None:
    demo = fleet({"demo": (app_spec(workflows={"nightly": {"orchestrator": "airflow", "schedule": "0 2 * * *", "tasks": {"run": {}}}}), {"dev": env_spec(workflows={"nightly": {"enabled": False}})})})
    instance = demo.instance("demo", "dev")
    assert instance is not None
    assert Setting("workflows.nightly", "disabled", "apps/demo/dev.yaml") in settings(demo, instance)


def test_explaining_an_environment_the_app_does_not_run_in_says_where_it_does() -> None:
    demo = fleet({"demo": (app_spec(), {"dev": env_spec(), "qa": env_spec()})})
    assert text(demo, "demo", "prod") == "demo does not run in prod: it runs in dev, qa\n"
