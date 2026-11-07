from collections.abc import Iterator

import pytest
from hypothesis import given
from hypothesis import strategies as st
from support import REPO, app_spec, dig, env_spec, fleet, items

from berth import gitio, yamlio
from berth.fleet import load
from berth.operate import targets
from berth.render import render
from berth.render.controlm import controlm_when
from berth.render.k8s import RenderError, merge_patch

REAL = load(gitio.worktree(REPO))


def _all_docs(files: dict[str, str]) -> Iterator[object]:
    for path, text in files.items():
        if path.endswith(".yaml") and path.startswith("k8s/"):
            yield from yamlio.load_all(text, source=path)


def test_rendering_is_deterministic() -> None:
    assert render(REAL) == render(REAL)


def test_each_app_environment_gets_one_manifest_file_and_each_cluster_its_platform_files() -> None:
    files = render(REAL)
    for instance in REAL.instances():
        cluster = REAL.platform.environments[instance.env].cluster
        assert f"k8s/{cluster}/{instance.namespace}/manifests.yaml" in files
    for cluster in REAL.platform.clusters:
        assert {f"k8s/{cluster}/_platform/{name}.yaml" for name in ("namespaces", "platform", "admission")} <= set(files)


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
    assert all(dig(doc, "metadata", "annotations", "berth.contoso.io/keep") == "true" for doc in kept)


def test_a_config_change_renames_the_config_map_so_pods_restart() -> None:
    one = fleet({"demo": (app_spec(config={"A": "1"}), {"dev": env_spec()})})
    two = fleet({"demo": (app_spec(config={"A": "2"}), {"dev": env_spec()})})
    path = "k8s/aks-qa/demo-dev/manifests.yaml"
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


def _docs_for(files: dict[str, str], path: str) -> list[object]:
    return list(yamlio.load_all(files[path], source=path))


def _container(files: dict[str, str], path: str, kind: str = "Deployment") -> dict[str, object]:
    (workload,) = [doc for doc in _docs_for(files, path) if dig(doc, "kind") == kind]
    (main,) = items(dig(workload, "spec", "template", "spec", "containers"))
    assert isinstance(main, dict)
    return {str(k): v for k, v in main.items()}


def _env(container: dict[str, object]) -> dict[str, str]:
    return {str(dig(var, "name")): str(dig(var, "value")) for var in items(container["env"]) if isinstance(var, dict) and "value" in var}


def test_every_container_gets_the_proxy_with_the_cluster_never_proxied() -> None:
    env = _env(_container(render(fleet()), "k8s/aks-qa/demo-dev/manifests.yaml"))
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://proxy.contoso.com:8080"
    bypass = env["NO_PROXY"].split(",")
    assert {".svc", ".cluster.local", "10.244.0.0/16", ".dev.apps.contoso.com", ".contoso.com"} <= set(bypass)
    assert env["no_proxy"] == env["NO_PROXY"]


def test_talks_to_gives_the_callers_host_port_and_url_on_the_targets_real_port() -> None:
    redis = app_spec(name="cache", services={"redis": {"port": 6379, "size": "s"}})
    caller = app_spec(talks_to=["cache/redis"])
    files = render(fleet({"cache": (redis, {"dev": env_spec()}), "demo": (caller, {"dev": env_spec()})}))
    env = _env(_container(files, "k8s/aks-qa/demo-dev/manifests.yaml"))
    assert env["CACHE_REDIS_HOST"] == "redis.cache-dev.svc.cluster.local"
    assert env["CACHE_REDIS_PORT"] == "6379"
    assert env["CACHE_REDIS_URL"] == "http://redis.cache-dev.svc.cluster.local:6379"
    (service,) = [doc for doc in _docs_for(files, "k8s/aks-qa/cache-dev/manifests.yaml") if dig(doc, "kind") == "Service"]
    assert dig(items(dig(service, "spec", "ports"))[0], "port") == 6379


def test_a_company_service_gets_a_route_on_its_environments_listener() -> None:
    spec = app_spec(services={"api": {"port": 8080, "expose": "company", "hostname": "quotes"}})
    files = render(fleet({"demo": (spec, {"qa": env_spec()})}))
    (route,) = [doc for doc in _docs_for(files, "k8s/aks-qa/demo-qa/manifests.yaml") if dig(doc, "kind") == "HTTPRoute"]
    assert dig(route, "spec", "hostnames") == ["quotes.qa.apps.contoso.com"]
    assert dig(items(dig(route, "spec", "parentRefs"))[0], "sectionName") == "https-qa"
    (gateway,) = [doc for doc in _docs_for(files, "k8s/aks-qa/_platform/platform.yaml") if dig(doc, "kind") == "Gateway"]
    listeners = {str(dig(item, "name")): item for item in items(dig(gateway, "spec", "listeners"))}
    assert set(listeners) == {"https-dev", "https-qa", "http"}
    assert dig(listeners["https-qa"], "hostname") == "*.qa.apps.contoso.com"
    assert dig(listeners["https-qa"], "allowedRoutes", "namespaces", "selector", "matchLabels", "berth.contoso.io/env") == "qa"


def test_writable_paths_get_an_empty_directory_each() -> None:
    spec = app_spec(services={"api": {"port": 8080, "writable": ["/var/run/redis", "/data"]}})
    files = render(fleet({"demo": (spec, {"dev": env_spec()})}))
    main = _container(files, "k8s/aks-qa/demo-dev/manifests.yaml")
    mounts = {str(dig(m, "mountPath")): str(dig(m, "name")) for m in items(main["volumeMounts"])}
    assert mounts["/var/run/redis"] == "writable-0"
    assert mounts["/data"] == "writable-1"


def test_owners_read_and_each_environments_break_glass_groups_may_change_workloads() -> None:
    files = render(fleet())
    groups = {}
    for env in ("dev", "prod"):
        cluster = "aks-prod" if env == "prod" else "aks-qa"
        docs = _docs_for(files, f"k8s/{cluster}/demo-{env}/manifests.yaml")
        bindings = {str(dig(doc, "metadata", "name")): doc for doc in docs if dig(doc, "kind") == "RoleBinding"}
        assert dig(bindings["berth-owners"], "roleRef", "name") == "view"
        assert dig(bindings["berth-break-glass"], "roleRef", "name") == "berth-break-glass"
        groups[env] = {str(dig(s, "name")) for s in items(dig(bindings["berth-break-glass"], "subjects"))}
    assert groups["dev"] != groups["prod"]  # production has its own group, managed by Entra PIM


def test_the_break_glass_role_cannot_read_secrets_open_a_shell_or_grant_access() -> None:
    (role,) = [doc for doc in _docs_for(render(fleet()), "k8s/aks-qa/_platform/platform.yaml") if dig(doc, "kind") == "ClusterRole" and dig(doc, "metadata", "name") == "berth-break-glass"]
    resources = {str(resource) for rule in items(dig(role, "rules")) for resource in items(dig(rule, "resources"))}
    assert {"deployments", "deployments/scale", "configmaps", "jobs"} <= resources
    assert not resources & {"secrets", "pods/exec", "pods/attach", "pods/portforward", "roles", "rolebindings", "namespaces"}


def test_settings_config_maps_are_versioned_so_deploys_can_retire_old_ones() -> None:
    docs = _docs_for(render(fleet({"demo": (app_spec(config={"A": "1"}, files={"a.toml": "x"}), {"dev": env_spec()})})), "k8s/aks-qa/demo-dev/manifests.yaml")
    maps = [doc for doc in docs if dig(doc, "kind") == "ConfigMap"]
    assert len(maps) == 2
    assert all(dig(doc, "metadata", "labels", "berth.contoso.io/versioned") == "true" for doc in maps)


def test_platform_software_namespaces_are_not_app_namespaces() -> None:
    files = render(REAL)
    for doc in _docs_for(files, "k8s/aks-qa/_platform/namespaces.yaml"):
        labels = dig(doc, "metadata", "labels")
        assert isinstance(labels, dict)
        assert "app.kubernetes.io/managed-by" not in labels  # so the app admission policies leave their pods alone


def test_a_deploy_target_knows_its_versioned_config_maps_without_parsing_them() -> None:
    demo = fleet({"demo": (app_spec(config={"A": "1"}, files={"a.toml": "x"}), {"dev": env_spec()})})
    files = render(demo)
    (target,) = targets(demo, "aks-qa", files)
    maps = [doc for doc in _docs_for(files, target.path) if dig(doc, "kind") == "ConfigMap"]
    rendered = {str(dig(doc, "metadata", "name")) for doc in maps}
    assert target.configmaps == rendered
    assert len(rendered) == 2
