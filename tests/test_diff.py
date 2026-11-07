from support import app_spec, env_spec, fleet, tree, version

from berth.diff import Change, affected_apps, flatten, show, spec_changes
from berth.fleet import load
from berth.gitio import changed_paths

ENVS = {"dev": env_spec(), "qa": env_spec(), "prod": env_spec()}


def _changes(before: dict[str, str], after: dict[str, str]) -> list[Change]:
    return spec_changes(load(before), load(after), changed_paths(before, after))


def test_flatten_keeps_lists_whole_and_drops_absent_values() -> None:
    assert flatten({"a": {"b": 1, "c": None}, "d": ["x", "y"], "e": [{"k": 1}]}) == {("a", "b"): 1, ("d",): ("x", "y"), ("e",): '[{"k": 1}]'}


def test_a_key_with_a_dot_stays_one_key() -> None:
    assert flatten({"files": {"indexer.toml": "x"}}) == {("files", "indexer.toml"): "x"}
    assert show(("files", "indexer.toml")) == "files[indexer.toml]"
    assert show(("services", "api", "size")) == "services.api.size"


def test_an_environment_file_change_is_reported_for_that_environment_only() -> None:
    before = tree()
    after = tree({"demo": (app_spec(), ENVS | {"prod": env_spec(config={"LOG_LEVEL": "warn"})})})
    assert _changes(before, after) == [Change("demo", "prod", ("config", "LOG_LEVEL"), None, "warn")]


def test_an_app_yaml_change_is_a_change_in_every_environment_it_reaches() -> None:
    before = tree()
    after = tree({"demo": (app_spec(config={"LOG_LEVEL": "info"}), ENVS)})
    assert {(c.env, c.path) for c in _changes(before, after)} == {(env, ("config", "LOG_LEVEL")) for env in ("dev", "qa", "prod")}


def test_new_and_retired_environments_and_apps() -> None:
    before = tree({"demo": (app_spec(), {"dev": env_spec()})})
    after = tree({"demo": (app_spec(), {"dev": env_spec(), "prod": env_spec()}), "fresh": (app_spec(name="fresh"), {"dev": env_spec()})})
    # A new app also starts in each of its environments, so its first prod deploy is judged like any other.
    added = [Change("demo", "prod", ("environment",), None, "added"), Change("fresh", None, ("app",), None, "added"), Change("fresh", "dev", ("environment",), None, "added")]
    assert _changes(before, after) == added
    retired = [Change("demo", "prod", ("environment",), "present", None), Change("fresh", None, ("app",), "present", None), Change("fresh", "dev", ("environment",), "present", None)]
    assert _changes(after, before) == retired


def test_a_platform_edit_is_not_attributed_to_the_apps_it_affects() -> None:
    before = tree()
    after = dict(before) | {"platform/platform.yaml": before["platform/platform.yaml"].replace("default_replicas: 2", "default_replicas: 3")}
    changes = _changes(before, after)
    assert changes == [Change(None, None, ("platform_file", "platform/platform.yaml"), None, "platform/platform.yaml")]


def test_other_files_in_an_app_directory_are_reported() -> None:
    before = tree()
    after = dict(before) | {"apps/demo/README.md": "# demo\n"}
    assert [c.path for c in _changes(before, after)] == [("repo_file", "README.md")]


def test_version_changes_are_ordinary_changes() -> None:
    before = tree()
    after = tree({"demo": (app_spec(), ENVS | {"dev": env_spec(version=version("1.1.0"))})})
    assert _changes(before, after) == [Change("demo", "dev", ("version",), version("1.0.0"), version("1.1.0"))]


def test_affected_apps_maps_rendered_paths_back_to_apps() -> None:
    base = head = fleet({"demo": (app_spec(), ENVS), "ledger": (app_spec(name="ledger"), {"prod": env_spec()})})
    paths = ["k8s/aks-qa/demo-dev/manifests.yaml", "k8s/aks-prod/ledger-prod/manifests.yaml", "k8s/aks-qa/_platform/platform.yaml", "airflow/qa/demo__nightly.py", "controlm/prod/ledger.json"]
    assert affected_apps(base, head, paths) == {"demo": {"dev", "qa"}, "ledger": {"prod"}}


def test_calling_another_app_is_a_change_to_that_app_too() -> None:
    ledger = app_spec(name="ledger", owners=["carol@contoso.com", "dev@contoso.com"])
    before = tree({"demo": (app_spec(), ENVS), "ledger": (ledger, ENVS)})
    after = tree({"demo": (app_spec(talks_to=["ledger/api"]), ENVS), "ledger": (ledger, ENVS)})
    new_callers = [c for c in _changes(before, after) if c.app == "ledger"]
    assert new_callers == [Change("ledger", env, ("callers", "demo"), None, "added") for env in ("dev", "prod", "qa")]
    assert not [c for c in _changes(after, before) if c.app == "ledger"]  # dropping a caller narrows access: nobody else asked
