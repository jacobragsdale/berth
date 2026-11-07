"""Platform software: the Helm values and the `helm template` command for each install. Pure: running Helm is
`berth.kube`'s job, at deploy time, because chart output (mostly CRDs) is megabytes nobody reviews line by line."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from berth.model import Platform, Software, SoftwareInstall
from berth.render.k8s import merge_patch

_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


@dataclass(frozen=True)
class Release:
    name: str  # the Helm release name, unique per cluster
    software: str
    spec: Software
    install: SoftwareInstall


def releases(software: Mapping[str, Software], cluster: str) -> list[Release]:
    """The cluster's installs in deploy order: by `order`, then by name."""
    found = [Release(name, key, item, install) for key, item in software.items() for name, install in item.installs.items() if install.cluster == cluster]
    return sorted(found, key=lambda release: (release.spec.order, release.name))


def placeholders(platform: Platform, cluster: str) -> dict[str, str]:
    """What values may refer to as ${name}, so a registry or tenant is written once, in platform.yaml."""
    return {"cluster": cluster, "tenant_id": platform.tenant_id, "registry": platform.registry, "key_vault": platform.clusters[cluster].key_vault, "gateway_namespace": platform.gateway.namespace}


def substitute(value: object, fields: Mapping[str, str]) -> object:
    if isinstance(value, str):

        def fill(match: re.Match[str]) -> str:
            if match[1] not in fields:
                msg = f"unknown placeholder ${{{match[1]}}}; use one of {', '.join(f'${{{name}}}' for name in sorted(fields))}"
                raise ValueError(msg)
            return fields[match[1]]

        return _PLACEHOLDER.sub(fill, value)
    if isinstance(value, Mapping):
        return {str(key): substitute(item, fields) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, fields) for item in value]
    return value


def values(release: Release, platform: Platform) -> dict[str, object]:
    merged = substitute(merge_patch(release.spec.values, release.install.values), placeholders(platform, release.install.cluster))
    return {str(key): item for key, item in merged.items()} if isinstance(merged, Mapping) else {}


def template_args(release: Release, kube_version: str | None) -> list[str]:
    """`helm template` arguments for one release, without the values file, which `berth.kube` supplies."""
    spec = release.spec
    location = ["--repo", spec.repo, spec.chart] if spec.repo.startswith("https://") else [f"{spec.repo}/{spec.chart}"]
    args = ["template", release.name, *location, "--version", spec.version, "--namespace", release.install.namespace, "--include-crds"]
    return args + (["--kube-version", kube_version] if kube_version is not None else [])
