"""Azure DevOps REST gateway: read a pull request and post its review status (the gate), and open or update the
bots' pull requests (`berth pr`).

Written against the documented REST API (7.1) but not yet run against a real organisation.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from berth.review import Decision, Proposal, Votes

API = "api-version=7.1"
STATUS_GENRE = "berth"
STATUS_NAME = "review"
_TIMEOUT_SECONDS = 30
_APPROVE = 5  # ADO votes: 10 approved, 5 approved with suggestions, 0 none, -5 waiting for author, -10 rejected
_REJECT = -10
TARGET = "refs/heads/main"
_DESCRIPTION_LIMIT = 4000  # Azure Repos refuses longer pull request descriptions

type Transport = Callable[[str, str, object | None], object]
"""(method, url, json body or None) -> parsed JSON response."""


class AdoError(RuntimeError):
    pass


class AdoSettings(BaseSettings):
    """The variables an Azure Pipelines job already has, plus the deploy repository when a job runs elsewhere.

    The gate maps System.AccessToken in explicitly. A bot signs in as its own identity and passes that token as
    BERTH_ADO_TOKEN (`az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798`).
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    system_collectionuri: str
    system_teamproject: str
    build_repository_id: str | None = None
    system_accesstoken: SecretStr | None = None
    berth_ado_token: SecretStr | None = None
    berth_ado_project: str | None = None  # the deploy repository's project, for a job that runs in another one
    berth_ado_repository: str | None = None  # the deploy repository's name or ID, for a job that checks out another

    @property
    def token(self) -> SecretStr:
        token = self.berth_ado_token if self.berth_ado_token is not None else self.system_accesstoken
        if token is None:
            msg = "no Azure DevOps token: set BERTH_ADO_TOKEN, or map System.AccessToken to SYSTEM_ACCESSTOKEN"
            raise AdoError(msg)
        return token

    @property
    def repository(self) -> str:
        repository = self.berth_ado_repository if self.berth_ado_repository is not None else self.build_repository_id
        if repository is None:
            msg = "which repository? set BERTH_ADO_REPOSITORY"
            raise AdoError(msg)
        return repository


def http_transport(token: SecretStr) -> Transport:
    def send(method: str, url: str, body: object | None) -> object:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - the URL is built from the pipeline's own https collection URI
        request.add_header("Authorization", f"Bearer {token.get_secret_value()}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 - see above
                parsed: object = json.loads(response.read() or b"null")
        except urllib.error.URLError as error:
            msg = f"Azure DevOps: {method} {url.split('?', 1)[0]}: {error}"
            raise AdoError(msg) from error
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


def _int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        msg = f"Azure DevOps response has no number {key!r}"
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
        project = settings.berth_ado_project if settings.berth_ado_project is not None else settings.system_teamproject
        organisation, repository = settings.system_collectionuri.rstrip("/"), quote(settings.repository)
        self._base = f"{organisation}/{quote(project)}/_apis/git/repositories/{repository}/pullRequests"
        self._send = transport

    def pull_request(self, pr_id: int) -> PullRequest:
        return parse_pull_request(pr_id, self._send("GET", f"{self._base}/{pr_id}?{API}", None))

    def post_status(self, pr_id: int, decision: Decision, target_url: str | None) -> None:
        body: dict[str, object] = {"state": decision.state, "description": decision.summary[:250], "context": {"genre": STATUS_GENRE, "name": STATUS_NAME}}
        if target_url is not None:
            body["targetUrl"] = target_url
        self._send("POST", f"{self._base}/{pr_id}/statuses?{API}", body)

    def propose(self, branch: str, title: str, description: str, *, auto_complete: bool) -> tuple[int, bool]:
        """Open a pull request from `branch` into main, or update the one already open, so a bot that runs again
        refreshes its proposal instead of adding another. Returns its ID and whether it is new."""
        source = f"refs/heads/{branch}"
        text = description[:_DESCRIPTION_LIMIT]
        query = urlencode({"searchCriteria.sourceRefName": source, "searchCriteria.targetRefName": TARGET, "searchCriteria.status": "active"})
        found = _mapping(self._send("GET", f"{self._base}?{query}&{API}", None), "pull requests").get("value")
        if isinstance(found, list) and found:
            pr_id = _int(_mapping(found[0], "pull request"), "pullRequestId")
            self._send("PATCH", f"{self._base}/{pr_id}?{API}", {"title": title, "description": text})
            return pr_id, False
        created = _mapping(self._send("POST", f"{self._base}?{API}", {"sourceRefName": source, "targetRefName": TARGET, "title": title, "description": text}), "pull request")
        pr_id = _int(created, "pullRequestId")
        if auto_complete:
            creator = _str(_mapping(created.get("createdBy"), "createdBy"), "id")
            self._send("PATCH", f"{self._base}/{pr_id}?{API}", {"autoCompleteSetBy": {"id": creator}, "completionOptions": {"deleteSourceBranch": True}})
        return pr_id, True
