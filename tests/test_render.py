from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st
from support import REPO, app_spec, env_spec, fleet

from berth import gitio, yamlio
from berth.fleet import load
from berth.render import render
from berth.render.controlm import controlm_when
from berth.render.k8s import RenderError, merge_patch

REAL = load(gitio.worktree(REPO))


def _all_docs(files: dict[str, str]) -> Iterator[object]:
    for path, text in files.items():
        if path.endswith(".yaml") and "/k8s/" in path:
            yield from yamlio.load_all(text, source=path)


def dig(value: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(value, dict), f"expected a mapping at {key!r}"
        value = {str(name): item for name, item in value.items()}[key]
    return value


def items(value: object) -> list[object]:
    assert isinstance(value, list)
    return list(value)


def test_rendering_is_deterministic() -> None:
    assert render(REAL) == render(REAL)


def test_each_app_environment_gets_one_manifest_file_and_one_flux_kustomization() -> None:
    files = render(REAL)
    for instance in REAL.instances():
        cluster = REAL.platform.environments[instance.env].cluster
        assert f"rendered/k8s/{cluster}/{instance.namespace}/manifests.yaml" in files
        assert f"rendered/k8s/{cluster}/_flux/{instance.namespace}.yaml" in files


def _pod_specs(doc: object) -> Iterator[object]:
    kind = dig(doc, "kind")
    if kind == "Deployment":
        yield dig(doc, "spec", "template", "spec")
    elif kind == "CronJob":
        yield dig(doc, "spec", "jobTemplate", "spec", "template", "spec")


def test_every_rendered_pod_runs_locked_down() -> None:
    pods = [pod for doc in _all_docs(render(REAL)) for pod in _pod_specs(doc)]
    assert pods
    for pod in pods:
        assert dig(pod, "securityContext", "runAsNonRoot") is True
        for container in items(dig(pod, "containers")):
            assert dig(container, "securityContext", "allowPrivilegeEscalation") is False
            assert dig(container, "securityContext", "capabilities") == {"drop": ["ALL"]}
            assert "@sha256:" in str(dig(container, "image"))


def test_data_bearing_objects_are_never_pruned_by_absence() -> None:
    kept = [doc for doc in _all_docs(render(REAL)) if dig(doc, "kind") in {"Namespace", "PersistentVolumeClaim"}]
    assert {dig(doc, "kind") for doc in kept} == {"Namespace", "PersistentVolumeClaim"}
    assert all(dig(doc, "metadata", "annotations", "kustomize.toolkit.fluxcd.io/prune") == "disabled" for doc in kept)


def test_a_config_change_renames_the_config_map_so_pods_restart() -> None:
    one = fleet({"demo": (app_spec(config={"A": "1"}), {"dev": env_spec()})})
    two = fleet({"demo": (app_spec(config={"A": "2"}), {"dev": env_spec()})})
    path = "rendered/k8s/aks-qa/demo-dev/manifests.yaml"
    name_one = next(dig(d, "metadata", "name") for d in _all_docs({path: render(one)[path]}) if dig(d, "kind") == "ConfigMap")
    name_two = next(dig(d, "metadata", "name") for d in _all_docs({path: render(two)[path]}) if dig(d, "kind") == "ConfigMap")
    assert name_one != name_two


def test_an_override_for_an_object_that_is_not_rendered_fails() -> None:
    spec = app_spec(overrides=[{"target": {"kind": "Deployment", "name": "demo-nope"}, "patch": {"spec": {"x": 1}}, "reason": "testing a missing target"}])
    with pytest.raises(RenderError, match="not rendered"):
        render(fleet({"demo": (spec, {"dev": env_spec()})}))


@pytest.mark.parametrize(
    ("target", "patch", "expected"),
    [
        ({"a": {"b": 1, "c": 2}}, {"a": {"b": 9}}, {"a": {"b": 9, "c": 2}}),
        ({"a": {"b": 1, "c": 2}}, {"a": {"c": None}}, {"a": {"b": 1}}),  # null deletes
        ({"a": [1, 2]}, {"a": [3]}, {"a": [3]}),  # lists replace, they do not merge
        ({"a": 1}, {"b": {"c": 1}}, {"a": 1, "b": {"c": 1}}),
    ],
)
def test_merge_patch(target: dict[str, object], patch: dict[str, object], expected: dict[str, object]) -> None:
    assert merge_patch(target, patch) == expected


@pytest.mark.parametrize(
    ("cron", "when"),
    [
        ("30 18 * * 1-5", {"FromTime": "1830", "WeekDays": ["MON", "TUE", "WED", "THU", "FRI"]}),
        ("0 2 * * *", {"FromTime": "0200", "WeekDays": ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]}),
        ("0 0 * * 0,7", {"FromTime": "0000", "WeekDays": ["SUN"]}),
        ("5 6 1,15 * *", {"FromTime": "0605", "MonthDays": ["1", "15"]}),
    ],
)
def test_controlm_translates_daily_cron(cron: str, when: dict[str, object]) -> None:
    assert controlm_when(cron) == when


@pytest.mark.parametrize(("cron", "message"), [("*/5 * * * *", "one fixed time"), ("0 2 * 1 *", "calendar"), ("0 2 1 * 1", "not both"), ("0 2 * * 1/2", "steps")])
def test_controlm_refuses_what_it_cannot_express(cron: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        controlm_when(cron)


@pytest.mark.parametrize("value", ["on", "Off", "yes", "NO", "y", "1:30", "0.99", "08", "1e3", "~", "null", "", ".inf"])
def test_strings_a_yaml_1_1_parser_would_misread_are_quoted(value: str) -> None:
    assert f'"{value}"' in yamlio.dump({"k": value})


@given(st.dictionaries(st.from_regex(r"[A-Z_][A-Z0-9_]{0,8}", fullmatch=True), st.text(max_size=30), max_size=5))
def test_config_values_round_trip_as_strings(config: dict[str, str]) -> None:
    assert yamlio.load(yamlio.dump({"data": config}), source="test") == {"data": config}


@pytest.mark.parametrize("value", ["\x1f\n", "tab\tthen\nnewline", "bell\x07"])
def test_control_characters_are_escaped_not_put_in_a_block(value: str) -> None:
    # Found by test_config_values_round_trip_as_strings: a literal block cannot hold control characters.
    assert yamlio.load(yamlio.dump({"k": value}), source="test") == {"k": value}
