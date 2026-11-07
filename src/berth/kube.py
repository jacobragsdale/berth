"""The only module that runs kubectl and Helm. It returns what they said as data; deciding what to do is elsewhere.

Every apply is server-side with the field manager `berth`, so the API server records which fields berth owns.
A field someone changed by hand (with `kubectl edit`, `scale`, `set`) belongs to another manager, so the next apply
stops with a conflict instead of silently undoing it, and a server-side diff shows it as drift.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

FIELD_MANAGER = "berth"
STAMP_MANAGER = "berth-deploy"  # owns the deployed-commit annotations, so they never show as drift
_TIMEOUT = 300


class Tools(BaseSettings):
    """Which binaries to run. Defaults to the ones on PATH; CI points them at the checksum-verified copies in .tools/."""

    model_config = SettingsConfigDict(env_prefix="BERTH_", extra="ignore", frozen=True)
    kubectl: str = "kubectl"
    helm: str = "helm"


class KubeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ManagedFields:
    """One entry of an object's `metadata.managedFields`: which fields one manager last set, as a `fieldsV1` tree."""

    manager: str
    operation: str
    subresource: str
    fields: Mapping[str, object]


@dataclass(frozen=True)
class ObjectRef:
    kind: str
    name: str
    namespace: str | None
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    owned: bool  # has ownerReferences: a controller made it, so it is not anyone's to manage
    managed_fields: tuple[ManagedFields, ...] = ()

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.kind, self.name, self.namespace)


@dataclass(frozen=True)
class Kubectl:
    binary: str
    context: str
    kubeconfig: str | None = None

    def run(self, *args: str, stdin: str | None = None) -> Result:
        env = dict(os.environ) | {"KUBECTL_EXTERNAL_DIFF": "diff -u -N"}  # the format berth.drift reads
        if self.kubeconfig is not None:
            env["KUBECONFIG"] = self.kubeconfig
        command = [self.binary, "--context", self.context, *args]
        try:
            done = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False, timeout=_TIMEOUT, env=env)
        except FileNotFoundError as error:
            msg = f"{self.binary} not found; install kubectl or set BERTH_KUBECTL"
            raise KubeError(msg) from error
        return Result(done.returncode, done.stdout, done.stderr)

    def _ok(self, *args: str, stdin: str | None = None) -> str:
        result = self.run(*args, stdin=stdin)
        if result.code != 0:
            msg = f"kubectl {' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}"
            raise KubeError(msg)
        return result.stdout

    def apply(self, manifests: str, *, force_conflicts: bool = False) -> Result:
        args = ["apply", "--server-side", f"--field-manager={FIELD_MANAGER}", "-f", "-"]
        if force_conflicts:
            args.append("--force-conflicts")
        return self.run(*args, stdin=manifests)

    def diff(self, manifests: str) -> Result:
        """Exit code 0 means the cluster matches, 1 means it differs, anything else is an error."""
        # --force-conflicts shows what reverting hand-made changes would do, rather than stopping at the conflict.
        return self.run("diff", "--server-side", f"--field-manager={FIELD_MANAGER}", "--force-conflicts", "-f", "-", stdin=manifests)

    def resource_types(self) -> set[str]:
        """Plural resource names the server knows, qualified by group (`deployments.apps`, `pods`)."""
        out = self._ok("api-resources", "--verbs=list", "-o", "name")
        return {line.strip() for line in out.splitlines() if line.strip()}

    def objects(self, types: Sequence[str], *, namespace: str | None = None, selector: str | None = None) -> list[ObjectRef]:
        if not types:
            return []
        args = ["get", ",".join(types), "-o", "json", "--show-managed-fields"]
        args += ["-n", namespace] if namespace is not None else []
        args += ["-l", selector] if selector is not None else []
        data: object = json.loads(self._ok(*args))
        return [_ref(item) for item in _items(data)]

    def patch(self, ref: ObjectRef, patch: Mapping[str, object]) -> None:
        """A strategic merge patch, which can delete list items by their key, under berth's own field manager."""
        args = ["patch", ref.kind, ref.name, "--type=strategic", "-p", json.dumps(patch), f"--field-manager={FIELD_MANAGER}"]
        self._ok(*args, *(["-n", ref.namespace] if ref.namespace is not None else []))

    def delete(self, ref: ObjectRef) -> None:
        args = ["delete", ref.kind, ref.name, "--wait=false"]
        self._ok(*args, *(["-n", ref.namespace] if ref.namespace is not None else []))

    def annotate_namespace(self, namespace: str, annotations: Mapping[str, str | None], *, manager: str) -> None:
        pairs = [f"{key}-" if value is None else f"{key}={value}" for key, value in annotations.items()]
        self._ok("annotate", "namespace", namespace, *pairs, "--overwrite", f"--field-manager={manager}")

    def namespaces(self, selector: str) -> list[ObjectRef]:
        return self.objects(["namespaces"], selector=selector)

    def rollout_status(self, namespace: str, deployment: str, timeout_seconds: int) -> Result:
        return self.run("rollout", "status", f"deployment/{deployment}", "-n", namespace, f"--timeout={timeout_seconds}s")

    def server_version(self) -> str:
        data: object = json.loads(self._ok("version", "-o", "json"))
        server = data.get("serverVersion") if isinstance(data, dict) else None
        version = server.get("gitVersion") if isinstance(server, dict) else None
        if not isinstance(version, str):
            msg = "kubectl version did not report the server's version"
            raise KubeError(msg)
        return version.lstrip("v").split("-", 1)[0]

    def whoami(self) -> str:
        return self._ok("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}").strip()


def _items(data: object) -> list[dict[str, object]]:
    if not isinstance(data, dict):
        return []
    items = data.get("items", [data] if "kind" in data and data.get("kind") != "List" else [])
    return [{str(key): value for key, value in item.items()} for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _strings(value: object) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _ref(item: Mapping[str, object]) -> ObjectRef:
    meta = item.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    namespace = meta.get("namespace")
    entries = meta.get("managedFields")
    managed = tuple(
        ManagedFields(str(entry.get("manager", "")), str(entry.get("operation", "")), str(entry.get("subresource", "")), _tree(entry.get("fieldsV1")))
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
    )
    return ObjectRef(
        kind=str(item.get("kind", "")),
        name=str(meta.get("name", "")),
        namespace=str(namespace) if namespace is not None else None,
        labels=_strings(meta.get("labels")),
        annotations=_strings(meta.get("annotations")),
        owned=bool(meta.get("ownerReferences")),
        managed_fields=managed,
    )


def _tree(value: object) -> dict[str, object]:
    return {str(key): _tree(child) for key, child in value.items()} if isinstance(value, dict) else {}


def helm_template(helm: str, args: Sequence[str], values: Mapping[str, object]) -> str:
    """Run `helm template` with arguments from `berth.render.software.template_args`, plus the values as a file."""
    with tempfile.TemporaryDirectory() as tmp:
        values_file = Path(tmp) / "values.json"
        values_file.write_text(json.dumps(values))
        try:
            done = subprocess.run([helm, *args, "--values", str(values_file)], capture_output=True, text=True, check=False, timeout=_TIMEOUT)
        except FileNotFoundError as error:
            msg = f"{helm} not found; install Helm or set BERTH_HELM"
            raise KubeError(msg) from error
    if done.returncode != 0:
        msg = f"helm template failed: {done.stderr.strip()}"
        raise KubeError(msg)
    return done.stdout
