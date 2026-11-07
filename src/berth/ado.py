"""Azure DevOps REST gateway for the merge gate: read a pull request, post its review status.

Written against the documented REST API (7.1) but not yet run against a real organisation.
"""

import json
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from berth.review import Decision, Proposal, Votes

API = "api-version=7.1"
STATUS_GENRE = "berth"
STATUS_NAME = "review"
_TIMEOUT_SECONDS = 30
_APPROVE = 5  # ADO votes: 10 approved, 5 approved with suggestions, 0 none, -5 waiting for author, -10 rejected
_REJECT = -10

type Transport = Callable[[str, str, object | None], object]
"""(method, url, json body or None) -> parsed JSON response."""


class AdoSettings(BaseSettings):
    """The variables an Azure Pipelines job already has; map System.AccessToken in explicitly."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    system_accesstoken: SecretStr
    system_collectionuri: str
    system_teamproject: str
    build_repository_id: str


class AdoError(RuntimeError):
    pass


def http_transport(token: SecretStr) -> Transport:
    def send(method: str, url: str, body: object | None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - the URL is built from the pipeline's own https collection URI
        request.add_header("Authorization", f"Bearer {token.get_secret_value()}")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 - see above
            parsed: object = json.loads(response.read() or b"null")
        return parsed

    return send


@dataclass(frozen=True)
class PullRequest:
    id: int
    proposal: Proposal


def _str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"Azure DevOps response has no string {key!r}"
        raise AdoError(msg)
    return value


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"Azure DevOps response: {what} is not an object"
        raise AdoError(msg)
    return {str(key): item for key, item in value.items()}


def parse_pull_request(pr_id: int, payload: object) -> PullRequest:
    data = _mapping(payload, "pull request")
    author = _str(_mapping(data.get("createdBy"), "createdBy"), "uniqueName").lower()
    approved: set[str] = set()
    rejected: set[str] = set()
    reviewers = data.get("reviewers", [])
    for raw in reviewers if isinstance(reviewers, list) else []:
        reviewer = _mapping(raw, "reviewer")
        if reviewer.get("isContainer") is True:
            continue  # a group's vote mirrors its members' votes; count people
        vote = reviewer.get("vote")
        name = _str(reviewer, "uniqueName").lower()
        if isinstance(vote, int) and vote >= _APPROVE:
            approved.add(name)
        elif isinstance(vote, int) and vote <= _REJECT:
            rejected.add(name)
    labels = data.get("labels", [])
    names = frozenset(_str(label, "name") for label in (_mapping(item, "label") for item in (labels if isinstance(labels, list) else [])) if label.get("active", True) is not False)
    return PullRequest(pr_id, Proposal(author, Votes(frozenset(approved), frozenset(rejected)), names))


class Ado:
    def __init__(self, settings: AdoSettings, transport: Transport) -> None:
        self._base = f"{settings.system_collectionuri.rstrip('/')}/{quote(settings.system_teamproject)}/_apis/git/repositories/{quote(settings.build_repository_id)}/pullRequests"
        self._send = transport

    def pull_request(self, pr_id: int) -> PullRequest:
        return parse_pull_request(pr_id, self._send("GET", f"{self._base}/{pr_id}?{API}", None))

    def post_status(self, pr_id: int, decision: Decision, target_url: str | None) -> None:
        body: dict[str, object] = {"state": decision.state, "description": decision.summary[:250], "context": {"genre": STATUS_GENRE, "name": STATUS_NAME}}
        if target_url is not None:
            body["targetUrl"] = target_url
        self._send("POST", f"{self._base}/{pr_id}/statuses?{API}", body)
