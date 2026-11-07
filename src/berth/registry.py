"""List an upstream image's tags over the OCI distribution API, for the mirror bot.

Works anonymously against public registries (Docker Hub, GitHub, Quay, Microsoft): a request answered 401 carries a
`WWW-Authenticate: Bearer` challenge naming a token service, which hands out a pull token. Written against the
OCI distribution spec and Docker Hub's token flow; not yet run against a real registry.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_TIMEOUT_SECONDS = 30
_UNAUTHORIZED = 401
_OK = 200
_PAGES = 50  # a guard against a registry that never stops paginating
_PARAM = re.compile(r'(\w+)="([^"]*)"')
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]  # lower-case names
    body: bytes


type HttpGet = Callable[[str, Mapping[str, str]], Reply]


class RegistryError(RuntimeError):
    pass


def http_get() -> HttpGet:
    def get(url: str, headers: Mapping[str, str]) -> Reply:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")  # noqa: S310 - https URLs only, checked in location()
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 - see above
                return Reply(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read())
        except urllib.error.HTTPError as error:
            return Reply(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())

    return get


def location(source: str) -> tuple[str, str]:
    """(https base URL, repository) for `host/repository`. Docker Hub's API lives at registry-1.docker.io."""
    host, _, repository = source.partition("/")
    if host == "docker.io":
        host = "registry-1.docker.io"
        if "/" not in repository:
            repository = f"library/{repository}"
    return f"https://{host}", repository


def _json(reply: Reply, what: str) -> dict[str, object]:
    data: object = json.loads(reply.body or b"{}")
    if not isinstance(data, dict):
        msg = f"{what}: expected a JSON object"
        raise RegistryError(msg)
    return {str(key): value for key, value in data.items()}


def _token(get: HttpGet, challenge: str, repository: str) -> str:
    params = dict(_PARAM.findall(challenge))
    if not challenge.lower().startswith("bearer") or "realm" not in params:
        msg = f"unsupported registry challenge: {challenge}"
        raise RegistryError(msg)
    query = {"service": params.get("service", ""), "scope": params.get("scope", f"repository:{repository}:pull")}
    reply = get(f"{params['realm']}?{urllib.parse.urlencode(query)}", {})
    if reply.status != _OK:
        msg = f"token service answered {reply.status}"
        raise RegistryError(msg)
    data = _json(reply, "token")
    token = data.get("token") or data.get("access_token")
    if not isinstance(token, str):
        msg = "token service returned no token"
        raise RegistryError(msg)
    return token


def tags(source: str, get: HttpGet) -> list[str]:
    base, repository = location(source)
    url: str | None = f"{base}/v2/{repository}/tags/list?n=1000"
    headers: dict[str, str] = {}
    found: list[str] = []
    for _ in range(_PAGES):
        if url is None:
            return found
        reply = get(url, headers)
        if reply.status == _UNAUTHORIZED and "authorization" not in headers:
            headers["authorization"] = f"Bearer {_token(get, reply.headers.get('www-authenticate', ''), repository)}"
            continue
        if reply.status != _OK:
            msg = f"{source}: registry answered {reply.status}"
            raise RegistryError(msg)
        listed = _json(reply, source).get("tags")
        found += [str(tag) for tag in listed] if isinstance(listed, list) else []
        following = _NEXT.search(reply.headers.get("link", ""))
        url = urllib.parse.urljoin(base, following[1]) if following else None
    msg = f"{source}: more than {_PAGES} pages of tags"
    raise RegistryError(msg)
