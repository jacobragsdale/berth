"""The only module that talks to a cluster or runs Helm. It returns what they said as data; deciding what to do is
elsewhere.

berth calls the API server's REST API through `kubectl proxy`, listening on a Unix socket in a private directory.
kubectl still handles the kubeconfig and the sign-in (kubelogin on AKS), and berth gets structured answers back: a
conflict arrives as the fields and managers involved, not as text to search for.

Every apply is server-side with the field manager `berth`, so the API server records which fields berth owns. A field
someone changed by hand (`kubectl edit`, `scale`, `set`) belongs to another manager, so the next apply stops with a
conflict instead of silently undoing it, and the managed-fields record shows it as drift.
"""

import http.client
import json
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import override
from urllib.parse import quote, urlencode

from pydantic_settings import BaseSettings, SettingsConfigDict

FIELD_MANAGER = "berth"
STAMP_MANAGER = "berth-deploy"  # owns the deployed-commit annotations, so they never show as drift
_REQUEST_SECONDS = 60
_PROXY_START_SECONDS = 30
_NEW_KIND_SECONDS = 30  # how long a kind from a CRD applied moments ago may take to be served
_PAGE = 500
# External Secrets reconciles a changed ExternalSecret within seconds. A Ready=False seen for longer than this is its
# answer for the version applied, not one left over from the version before.
SECRET_GRACE_SECONDS = 20.0
# Aggregated discovery (Kubernetes 1.30+): every group, version and resource in two requests.
_DISCOVERY = "application/json;g=apidiscovery.k8s.io;v=v2;as=APIGroupDiscoveryList,application/json"
_APPLY = "application/apply-patch+yaml"  # JSON is YAML, so berth sends JSON
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_HTTP_ERROR = 300


class Tools(BaseSettings):
    """Which binaries to run. Defaults to the ones on PATH; CI points them at the checksum-verified copies in .tools/."""

    model_config = SettingsConfigDict(env_prefix="BERTH_", extra="ignore", frozen=True)
    kubectl: str = "kubectl"
    helm: str = "helm"


class KubeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Resource:
    """One kind the API server serves, from discovery."""

    group: str
    version: str
    plural: str
    kind: str
    namespaced: bool

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}" if self.group else self.version

    @property
    def type_name(self) -> str:
        """The name kubectl uses: `deployments.apps`, or just `pods` for the core group."""
        return f"{self.plural}.{self.group}" if self.group else self.plural

    def path(self, namespace: str | None = None, name: str | None = None) -> str:
        base = f"/apis/{self.group}/{self.version}" if self.group else f"/api/{self.version}"
        if self.namespaced and namespace is not None:
            base += f"/namespaces/{quote(namespace)}"
        base += f"/{self.plural}"
        return base if name is None else f"{base}/{quote(name)}"


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
    api_version: str = ""
    body: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)  # the whole object

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.kind, self.name, self.namespace)

    @staticmethod
    def of(api_version: str, kind: str, name: str, namespace: str | None = None) -> "ObjectRef":
        """A reference to an object by name, to act on it without having read it."""
        return ObjectRef(kind, name, namespace, {}, {}, owned=False, api_version=api_version)


@dataclass(frozen=True)
class Conflict:
    """A field someone else owns that an apply would change, as the API server reported it."""

    field: str  # .spec.replicas
    message: str  # conflict with "kubectl" with subresource "scale" using apps/v1


@dataclass(frozen=True)
class Applied:
    """The answer to one apply: the object as stored (or as it would be, for a dry run), or why not."""

    kind: str
    name: str
    stored: Mapping[str, object] | None = None
    conflicts: tuple[Conflict, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.stored is not None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=_REQUEST_SECONDS)
        self._socket_path = socket_path

    @override
    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_REQUEST_SECONDS)
        sock.connect(self._socket_path)
        self.sock = sock


def _mapping(value: object) -> dict[str, object]:
    return {str(key): item for key, item in value.items()} if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _status_message(payload: object, status: int) -> str:
    message = _mapping(payload).get("message")
    return str(message) if message else f"the API server answered {status}"


class ApiServer:
    """A cluster's API server, through `kubectl proxy`. Safe to use from several threads: each request is its own
    connection to the proxy, and the proxy multiplexes them onto one connection to the cluster."""

    def __init__(self, socket_path: str) -> None:
        self._socket = socket_path
        self._lock = threading.Lock()
        self._by_kind: dict[tuple[str, str], Resource] = {}
        self._by_type: dict[str, Resource] = {}

    @staticmethod
    @contextmanager
    def connect(tools: Tools, context: str, kubeconfig: str | None = None) -> Generator["ApiServer"]:
        """Start `kubectl proxy` for one kubeconfig context and yield a client for it. Only this user can reach the
        socket: it lives in a fresh directory that only its owner may open."""
        with tempfile.TemporaryDirectory(prefix="berth-") as private:
            socket_path = str(Path(private) / "kube.sock")
            command = [tools.kubectl, "--context", context, *(["--kubeconfig", kubeconfig] if kubeconfig is not None else []), "proxy", f"--unix-socket={socket_path}"]
            try:
                proxy = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            except FileNotFoundError as error:
                msg = f"{tools.kubectl} not found; install kubectl or set BERTH_KUBECTL"
                raise KubeError(msg) from error
            with proxy:  # closes its pipe and reaps it on the way out
                try:
                    api = ApiServer(socket_path)
                    api.wait_ready(proxy)
                    yield api
                finally:
                    proxy.terminate()
                    with suppress(subprocess.TimeoutExpired):
                        proxy.wait(timeout=10)
                    if proxy.poll() is None:
                        proxy.kill()

    def wait_ready(self, proxy: "subprocess.Popen[str]") -> None:
        deadline = time.monotonic() + _PROXY_START_SECONDS
        while True:
            if proxy.poll() is not None:
                error = proxy.stderr.read().strip() if proxy.stderr is not None else ""
                msg = f"kubectl proxy stopped: {error or f'exit code {proxy.returncode}'}"
                raise KubeError(msg)
            if Path(self._socket).exists():
                with suppress(OSError, KubeError):
                    self.server_version()
                    return
            if time.monotonic() > deadline:
                msg = "kubectl proxy did not start; check the kubeconfig context and that you are signed in"
                raise KubeError(msg)
            time.sleep(0.05)

    def request(self, method: str, path: str, *, query: Mapping[str, str] | None = None, body: object = None, headers: Mapping[str, str] | None = None) -> tuple[int, object]:
        """One call to the API server. `headers` adds to (or replaces) JSON in both directions."""
        url = path if query is None else f"{path}?{urlencode(query)}"
        connection = _UnixConnection(self._socket)
        try:
            payload = None if body is None else json.dumps(body)
            connection.request(method, url, body=payload, headers={"Content-Type": "application/json", "Accept": "application/json"} | dict(headers or {}))
            response = connection.getresponse()
            data = response.read()
        except OSError as error:
            msg = f"{method} {path}: {error}"
            raise KubeError(msg) from error
        finally:
            connection.close()
        try:
            parsed: object = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = {"message": data.decode(errors="replace").strip()}
        return response.status, parsed

    def _ok(self, method: str, path: str, *, query: Mapping[str, str] | None = None, body: object = None, headers: Mapping[str, str] | None = None) -> object:
        status, payload = self.request(method, path, query=query, body=body, headers=headers)
        if status >= _HTTP_ERROR:
            msg = f"{method} {path}: {_status_message(payload, status)}"
            raise KubeError(msg)
        return payload

    # Discovery

    def _discover(self) -> None:
        by_kind: dict[tuple[str, str], Resource] = {}
        by_type: dict[str, Resource] = {}
        for root in ("/api", "/apis"):
            payload = self._ok("GET", root, headers={"Accept": _DISCOVERY})
            if _mapping(payload).get("kind") != "APIGroupDiscoveryList":
                msg = "the API server does not offer aggregated discovery; berth needs Kubernetes 1.30 or later"
                raise KubeError(msg)
            for group in _list(_mapping(payload).get("items")):
                name = str(_mapping(_mapping(group).get("metadata")).get("name", ""))
                for version in _list(_mapping(group).get("versions")):  # the preferred version comes first
                    for item in _list(_mapping(version).get("resources")):
                        entry = _mapping(item)
                        kind = _mapping(entry.get("responseKind")).get("kind")
                        if not kind:
                            continue
                        resource = Resource(name, str(_mapping(version).get("version")), str(entry.get("resource")), str(kind), entry.get("scope") == "Namespaced")
                        by_kind.setdefault((resource.api_version, resource.kind), resource)
                        by_type.setdefault(resource.type_name, resource)
        with self._lock:
            self._by_kind, self._by_type = by_kind, by_type

    def resource(self, api_version: str, kind: str) -> Resource:
        """The resource for a kind, waiting briefly for one whose CRD was applied moments ago."""
        deadline = time.monotonic() + _NEW_KIND_SECONDS
        while True:
            with self._lock:
                found = self._by_kind.get((api_version, kind))
            if found is not None:
                return found
            if time.monotonic() > deadline:
                msg = f"the cluster does not serve {kind} ({api_version}); is its CRD installed?"
                raise KubeError(msg)
            self._discover()
            with self._lock:
                if (api_version, kind) in self._by_kind:
                    continue
            time.sleep(1)

    def resource_type(self, name: str) -> Resource | None:
        """A resource by kubectl's name for it (`deployments.apps`), or None if the cluster does not serve it."""
        with self._lock:
            known = bool(self._by_type)
        if not known:
            self._discover()
        with self._lock:
            return self._by_type.get(name)

    # Reads

    def get(self, resource: Resource, name: str, namespace: str | None = None) -> dict[str, object] | None:
        status, payload = self.request("GET", resource.path(namespace, name))
        if status == _HTTP_NOT_FOUND:
            return None
        if status >= _HTTP_ERROR:
            msg = f"GET {resource.kind}/{name}: {_status_message(payload, status)}"
            raise KubeError(msg)
        return _mapping(payload)

    def list(self, resource: Resource, *, namespace: str | None = None, labels: str | None = None, fields: str | None = None) -> list[ObjectRef]:
        """Every object of one kind, across all namespaces unless one is given, a page at a time."""
        query = {"limit": str(_PAGE)} | ({"labelSelector": labels} if labels is not None else {}) | ({"fieldSelector": fields} if fields is not None else {})
        found: list[ObjectRef] = []
        while True:
            page = _mapping(self._ok("GET", resource.path(namespace), query=query))
            found += [ref(_mapping(item) | {"kind": resource.kind, "apiVersion": resource.api_version}) for item in _list(page.get("items"))]
            more = _mapping(page.get("metadata")).get("continue")
            if not more:
                return found
            query = query | {"continue": str(more)}

    # Writes

    def apply(self, doc: Mapping[str, object], *, manager: str = FIELD_MANAGER, force: bool = False, dry_run: bool = False, default_namespace: str | None = None) -> Applied:
        """Server-side apply of one object. A conflict is an answer, not an error: it says who owns what."""
        kind, api_version = str(doc.get("kind", "")), str(doc.get("apiVersion", ""))
        meta = _mapping(doc.get("metadata"))
        name = str(meta.get("name", ""))
        try:
            resource = self.resource(api_version, kind)
        except KubeError as error:
            return Applied(kind, name, error=str(error))
        written = meta.get("namespace")
        namespace = (str(written) if written is not None else default_namespace) if resource.namespaced else None
        if resource.namespaced and namespace is None:
            return Applied(kind, name, error=f"{kind}/{name} has no namespace")
        if namespace is not None and written is None:
            doc = dict(doc) | {"metadata": meta | {"namespace": namespace}}  # as kubectl apply -n would
        query = {"fieldManager": manager} | ({"force": "true"} if force else {}) | ({"dryRun": "All"} if dry_run else {})
        status, payload = self.request("PATCH", resource.path(namespace, name), query=query, body=doc, headers={"Content-Type": _APPLY})
        if status < _HTTP_ERROR:
            return Applied(kind, name, stored=_mapping(payload))
        if status == _HTTP_CONFLICT:
            causes = _list(_mapping(_mapping(payload).get("details")).get("causes"))
            conflicts = tuple(Conflict(str(_mapping(cause).get("field", "")), str(_mapping(cause).get("message", ""))) for cause in causes)
            if conflicts:
                return Applied(kind, name, conflicts=conflicts)
        return Applied(kind, name, error=_status_message(payload, status))

    def patch(self, target: ObjectRef, patch: Mapping[str, object]) -> None:
        """A strategic merge patch, which can delete list items by their key, under berth's own field manager."""
        resource = self.resource(target.api_version, target.kind)
        path = resource.path(target.namespace, target.name)
        self._ok("PATCH", path, query={"fieldManager": FIELD_MANAGER}, body=dict(patch), headers={"Content-Type": "application/strategic-merge-patch+json"})

    def delete(self, target: ObjectRef) -> None:
        resource = self.resource(target.api_version, target.kind)
        status, payload = self.request("DELETE", resource.path(target.namespace, target.name), query={"propagationPolicy": "Background"})
        if status >= _HTTP_ERROR and status != _HTTP_NOT_FOUND:
            msg = f"DELETE {target.kind}/{target.name}: {_status_message(payload, status)}"
            raise KubeError(msg)

    def annotate(self, target: ObjectRef, annotations: Mapping[str, str], *, manager: str) -> None:
        """Own exactly these annotations as `manager`. Annotations the manager set before and leaves out are removed."""
        meta: dict[str, object] = {"name": target.name, "annotations": dict(annotations)}
        if target.namespace is not None:
            meta["namespace"] = target.namespace
        applied = self.apply({"apiVersion": target.api_version, "kind": target.kind, "metadata": meta}, manager=manager, force=True)
        if not applied.ok:
            msg = f"annotate {target.kind}/{target.name}: {applied.error}"
            raise KubeError(msg)

    # Facts about the cluster

    def server_version(self) -> str:
        version = _mapping(self._ok("GET", "/version")).get("gitVersion")
        if not isinstance(version, str):
            msg = "the API server did not report its version"
            raise KubeError(msg)
        return version.lstrip("v").split("-", 1)[0]

    def whoami(self) -> tuple[str, frozenset[str]]:
        """The signed-in user and their groups, as the cluster sees them (Entra group object IDs on AKS)."""
        review = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
        info = _mapping(_mapping(_mapping(self._ok("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews", body=review)).get("status")).get("userInfo"))
        return str(info.get("username", "?")), frozenset(str(group) for group in _list(info.get("groups")))

    def rollout(self, namespace: str, name: str, timeout_seconds: float, *, poll_seconds: float = 2.0) -> str | None:
        """Wait for a Deployment's rollout, as `kubectl rollout status` does. None when complete, otherwise why not."""
        resource = self.resource("apps/v1", "Deployment")
        deadline = time.monotonic() + timeout_seconds
        while True:
            deployment = self.get(resource, name, namespace)
            if deployment is None:
                return "the Deployment does not exist"
            done, why = rollout_state(deployment)
            if done:
                return None
            if why.startswith("failed") or time.monotonic() > deadline:
                return why
            time.sleep(poll_seconds)

    def secret_synced(self, namespace: str, name: str, generation: int, deadline: float, *, poll_seconds: float = 2.0) -> str | None:
        """Wait, until the monotonic `deadline`, for External Secrets to sync this generation of an ExternalSecret.
        None when it did, otherwise why not, with the operator's own message (which names a missing key)."""
        resource = self.resource("external-secrets.io/v1", "ExternalSecret")
        failing_since: float | None = None
        while True:
            found = self.get(resource, name, namespace)
            if found is None:
                return "the ExternalSecret does not exist"
            synced, why = sync_state(found, generation)
            if synced is True:
                return None
            now = time.monotonic()
            failing_since = (failing_since or now) if synced is False else None
            if now > deadline or (failing_since is not None and now - failing_since >= SECRET_GRACE_SECONDS):
                return why
            time.sleep(poll_seconds)


def sync_state(external_secret: Mapping[str, object], generation: int) -> tuple[bool | None, str]:
    """Whether External Secrets synced this generation of an ExternalSecret: True, False (it reported Ready=False), or
    None (not yet), with why. A sync records `syncedResourceVersion` as `<generation>-<hash of the metadata>`."""
    status = _mapping(external_secret.get("status"))
    ready = next((_mapping(item) for item in _list(status.get("conditions")) if _mapping(item).get("type") == "Ready"), None)
    if ready is None:
        return None, "External Secrets has not reported on it"
    message = str(ready.get("message") or ready.get("reason") or "no message")
    if ready.get("status") == "False":
        return False, f"not synced: {message}"
    if ready.get("status") == "True" and str(status.get("syncedResourceVersion", "")).startswith(f"{generation}-"):
        return True, ""
    return None, f"External Secrets has not synced this version yet (last: {message})"


def _count(values: Mapping[str, object], key: str, default: int = 0) -> int:
    value = values.get(key, default)
    return value if isinstance(value, int) else default


def rollout_state(deployment: Mapping[str, object]) -> tuple[bool, str]:
    """Whether a Deployment has finished rolling out, by the rules `kubectl rollout status` uses."""
    meta, spec, status = (_mapping(deployment.get(part)) for part in ("metadata", "spec", "status"))
    if _count(status, "observedGeneration") < _count(meta, "generation"):
        return False, "waiting for the Deployment controller"
    for condition in _list(status.get("conditions")):
        entry = _mapping(condition)
        if entry.get("type") == "Progressing" and entry.get("reason") == "ProgressDeadlineExceeded":
            return False, f"failed: {entry.get('message', 'progress deadline exceeded')}"
    wanted, updated = _count(spec, "replicas", 1), _count(status, "updatedReplicas")
    current, available = _count(status, "replicas"), _count(status, "availableReplicas")
    if updated < wanted:
        return False, f"{updated} of {wanted} new pods created"
    if current > updated:
        return False, f"{current - updated} old pods still running"
    if available < updated:
        return False, f"{available} of {updated} new pods ready"
    return True, ""


def ref(item: Mapping[str, object]) -> ObjectRef:
    meta = _mapping(item.get("metadata"))
    namespace = meta.get("namespace")
    managed = tuple(
        ManagedFields(str(entry.get("manager", "")), str(entry.get("operation", "")), str(entry.get("subresource", "")), _tree(entry.get("fieldsV1")))
        for entry in (_mapping(raw) for raw in _list(meta.get("managedFields")))
    )
    return ObjectRef(
        kind=str(item.get("kind", "")),
        name=str(meta.get("name", "")),
        namespace=str(namespace) if namespace is not None else None,
        labels={str(k): str(v) for k, v in _mapping(meta.get("labels")).items()},
        annotations={str(k): str(v) for k, v in _mapping(meta.get("annotations")).items()},
        owned=bool(meta.get("ownerReferences")),
        managed_fields=managed,
        api_version=str(item.get("apiVersion", "")),
        body=dict(item),
    )


def _tree(value: object) -> dict[str, object]:
    return {str(key): _tree(child) for key, child in value.items()} if isinstance(value, dict) else {}


def helm_template(helm: str, args: Sequence[str], values: Mapping[str, object]) -> str:
    """Run `helm template` with arguments from `berth.render.software.template_args`, plus the values as a file."""
    with tempfile.TemporaryDirectory() as tmp:
        values_file = Path(tmp) / "values.json"
        values_file.write_text(json.dumps(values))
        try:
            done = subprocess.run([helm, *args, "--values", str(values_file)], capture_output=True, text=True, check=False, timeout=300)
        except FileNotFoundError as error:
            msg = f"{helm} not found; install Helm or set BERTH_HELM"
            raise KubeError(msg) from error
    if done.returncode != 0:
        msg = f"helm template failed: {done.stderr.strip()}"
        raise KubeError(msg)
    return done.stdout
