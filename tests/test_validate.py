"""Rules across files: one hostname per environment, and platform software that fits around the apps."""

from support import app_spec, env_spec, tree

from berth.fleet import load
from berth.validate import problems

ENVOY = """
description: test gateway
repo: oci://docker.io/envoyproxy
chart: gateway-helm
version: v1.9.3
values: {image: "${registry}/mirror/envoy"}
installs:
  envoy-gateway:
    cluster: aks-qa
    namespace: envoy-gateway-system
    secrets:
      berth-dev-tls: {type: kubernetes.io/tls, data: {tls.crt: a, tls.key: b}}
      berth-qa-tls: {type: kubernetes.io/tls, data: {tls.crt: c, tls.key: d}}
"""


def _problems(files: dict[str, str]) -> list[str]:
    return problems(load(files))


def test_two_services_cannot_share_a_hostname_in_one_environment() -> None:
    web = {"port": 8080, "expose": "company", "hostname": "quotes"}
    files = tree({"one": (app_spec(name="one", services={"web": web}), {"dev": env_spec()}), "two": (app_spec(name="two", services={"web": web}), {"dev": env_spec()})})
    assert any("'quotes' is already used in dev by one/web" in problem for problem in _problems(files))


def test_the_same_hostname_in_different_environments_is_fine() -> None:
    web = {"port": 8080, "expose": "company", "hostname": "quotes"}
    files = tree({"one": (app_spec(name="one", services={"web": web}), {"dev": env_spec()}), "two": (app_spec(name="two", services={"web": web}), {"qa": env_spec()})})
    assert not _problems(files)


def test_valid_platform_software_passes() -> None:
    assert not _problems(tree() | {"platform/software/envoy-gateway.yaml": ENVOY})


def test_an_unknown_placeholder_in_software_values_is_reported() -> None:
    found = _problems(tree() | {"platform/software/envoy-gateway.yaml": ENVOY.replace("${registry}", "${nope}")})
    assert any("unknown placeholder ${nope}" in problem for problem in found)


def test_every_environment_on_a_cluster_needs_its_gateway_certificate() -> None:
    found = _problems(tree() | {"platform/software/envoy-gateway.yaml": ENVOY.replace("      berth-qa-tls: {type: kubernetes.io/tls, data: {tls.crt: c, tls.key: d}}\n", "")})
    assert any("certificate 'berth-qa-tls'" in problem for problem in found)


def test_software_cannot_take_an_app_namespace() -> None:
    found = _problems(tree() | {"platform/software/envoy-gateway.yaml": ENVOY.replace("namespace: envoy-gateway-system", "namespace: demo-dev")})
    assert any("'demo-dev' is already an app namespace" in problem for problem in found)
