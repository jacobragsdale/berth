"""Admission rules, enforced by the API server itself with ValidatingAdmissionPolicy (built in since Kubernetes 1.30).

They repeat what the renderer already does, for whatever reaches the cluster without it: a break-glass `kubectl
edit`, or a pod that Airflow or Control-M starts. Nothing is installed to enforce them; a policy is just an object.
Each policy applies only in namespaces labelled as berth's, through its binding. Platform software is exempt.

Expressions are CEL. A policy is compiled against the schema of each kind it matches, so a policy reads the pod spec
from one place: one policy per workload shape rather than one policy with a branch per kind.
"""

import json
from collections.abc import Mapping, Sequence

from berth.model import Platform
from berth.render.pod import LABEL_APP, LABEL_ENV, MANAGED, Doc

API = "admissionregistration.k8s.io/v1"
WRITES = ["CREATE", "UPDATE"]
APPS = MANAGED  # policies apply in namespaces labelled as berth's

# Where each kind keeps its pod spec. Deployments, Jobs and friends share a shape; CronJobs nest one level deeper.
_SHAPES = {
    "pods": ([("", "pods")], "object.spec"),
    "templates": ([("apps", "deployments"), ("apps", "statefulsets"), ("apps", "daemonsets"), ("apps", "replicasets"), ("batch", "jobs")], "object.spec.template.spec"),
    "cronjobs": ([("batch", "cronjobs")], "object.spec.jobTemplate.spec.template.spec"),
}


def _rules(resources: list[tuple[str, str]]) -> list[Doc]:
    groups: dict[str, list[str]] = {}
    for group, resource in resources:
        groups.setdefault(group, []).append(resource)
    return [{"apiGroups": [group], "apiVersions": ["*"], "operations": WRITES, "resources": names} for group, names in groups.items()]


def _policy(name: str, resources: list[tuple[str, str]], validations: Sequence[Mapping[str, str]], variables: Sequence[Mapping[str, str]] = ()) -> list[Doc]:
    spec: Doc = {"failurePolicy": "Fail", "matchConstraints": {"resourceRules": _rules(resources)}}
    if variables:
        spec["variables"] = [dict(variable) for variable in variables]
    spec["validations"] = [dict(validation) for validation in validations]
    return [
        {"apiVersion": API, "kind": "ValidatingAdmissionPolicy", "metadata": {"name": name, "labels": APPS}, "spec": spec},
        {
            "apiVersion": API,
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name, "labels": APPS},
            "spec": {"policyName": name, "validationActions": ["Deny"], "matchResources": {"namespaceSelector": {"matchLabels": APPS}}},
        },
    ]


def _cel(value: str) -> str:
    return json.dumps(value)  # a JSON string literal is a valid CEL string literal


def policies(platform: Platform) -> list[Doc]:
    registry = _cel(f"{platform.registry}/")
    docs: list[Doc] = []
    for shape, (resources, spec) in _SHAPES.items():
        containers = [
            {"name": "spec", "expression": spec},
            {"name": "containers", "expression": "variables.spec.containers + (has(variables.spec.initContainers) ? variables.spec.initContainers : [])"},
        ]
        docs += _policy(
            f"berth-images-{shape}",
            resources,
            [
                {
                    "expression": f"variables.containers.all(c, c.image.startsWith({registry}) && c.image.contains('@sha256:'))",
                    "message": f"images must come from {platform.registry} and be pinned by digest (tag@sha256:...)",
                }
            ],
            containers,
        )
        docs += _policy(
            f"berth-resources-{shape}",
            resources,
            [
                {
                    "expression": "variables.containers.all(c, has(c.resources) && has(c.resources.requests) && has(c.resources.limits)"
                    " && 'cpu' in c.resources.requests && 'memory' in c.resources.requests && 'memory' in c.resources.limits)",
                    "message": "every container needs CPU and memory requests and a memory limit",
                }
            ],
            containers,
        )
    app, env = _cel(LABEL_APP), _cel(LABEL_ENV)
    docs += _policy(
        "berth-external-secrets",
        [("external-secrets.io", "externalsecrets")],
        [
            {"expression": "!has(object.spec.dataFrom)", "message": "dataFrom can read a whole vault; list each secret under data"},
            {
                "expression": f"object.spec.secretStoreRef.kind == 'ClusterSecretStore' && object.spec.secretStoreRef.name == 'berth-' + namespaceObject.metadata.labels[{env}]",
                "message": "an app reads secrets only from its own environment's store",
            },
            {
                "expression": f"!has(object.spec.data) || object.spec.data.all(d, d.remoteRef.key.startsWith(namespaceObject.metadata.labels[{app}] + '--'))",
                "message": "an app reads only its own secrets: keys must start with <app>--",
            },
        ],
    )
    domains = "{" + ", ".join(f"{_cel(name)}: {_cel('.' + environment.domain)}" for name, environment in sorted(platform.environments.items())) + "}"
    docs += _policy(
        "berth-routes",
        [("gateway.networking.k8s.io", "httproutes")],
        [
            {"expression": f"object.spec.hostnames.all(h, h.endsWith({domains}[namespaceObject.metadata.labels[{env}]]))", "message": "a route's hostnames must be under its own environment's domain"},
            {"expression": "object.spec.parentRefs.all(p, p.name == 'berth')", "message": "routes attach only to the berth Gateway"},
        ],
    )
    owners = _cel(f"{LABEL_APP}-owners")
    docs += _policy(
        "berth-owners",
        [("", "namespaces")],
        [
            {
                "expression": f"has(object.metadata.annotations) && {owners} in object.metadata.annotations && object.metadata.annotations[{owners}].split(', ').size() >= 2",
                "message": "every app namespace names at least two owners",
            }
        ],
    )
    return docs
