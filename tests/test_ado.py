"""The Azure DevOps gateway, against payloads shaped like the documented REST API (7.1).

These are not recorded from a real organisation; the first live run should replace them with trimmed captures.
"""

from pydantic import SecretStr

from berth.ado import Ado, AdoSettings, parse_pull_request
from berth.review import Decision

PR = {
    "pullRequestId": 42,
    "createdBy": {"uniqueName": "Ann@Contoso.com"},
    "reviewers": [
        {"uniqueName": "ben@contoso.com", "vote": 10},
        {"uniqueName": "infra.lead@contoso.com", "vote": 5},
        {"uniqueName": "carl@contoso.com", "vote": -5},
        {"uniqueName": "dee@contoso.com", "vote": -10},
        {"uniqueName": "[contoso]\\Platform", "vote": 10, "isContainer": True},
    ],
    "labels": [{"name": "break-glass", "active": False}, {"name": "hotfix", "active": True}],
}


def test_votes_labels_and_author_are_read_from_the_pull_request() -> None:
    proposal = parse_pull_request(42, PR).proposal
    assert proposal.author == "ann@contoso.com"
    assert proposal.votes.approved == {"ben@contoso.com", "infra.lead@contoso.com"}  # "approved with suggestions" counts
    assert proposal.votes.rejected == {"dee@contoso.com"}  # "waiting for author" is not a rejection
    assert proposal.labels == {"hotfix"}  # removed labels stay in the payload as inactive


def test_the_gate_posts_one_berth_review_status() -> None:
    calls: list[tuple[str, str, object]] = []

    def transport(method: str, url: str, body: object | None) -> object:
        calls.append((method, url, body))
        return PR if method == "GET" else {}

    settings = AdoSettings(system_accesstoken=SecretStr("t"), system_collectionuri="https://dev.azure.com/contoso/", system_teamproject="Platform Team", build_repository_id="repo-1")
    ado = Ado(settings, transport)
    ado.pull_request(42)
    ado.post_status(42, Decision("pending", ("demo: needs approval from an owner",)), "https://example.test/run/1")
    method, url, body = calls[-1]
    assert method == "POST"
    assert url == "https://dev.azure.com/contoso/Platform%20Team/_apis/git/repositories/repo-1/pullRequests/42/statuses?api-version=7.1"
    assert body == {"state": "pending", "description": "demo: needs approval from an owner", "context": {"genre": "berth", "name": "review"}, "targetUrl": "https://example.test/run/1"}
