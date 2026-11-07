"""The mirror bot: which upstream tag to take, which apps to release it to, and how tags are listed."""

import json
from collections.abc import Mapping

import pytest
from support import app_spec, env_spec, tree

from berth.fleet import Fleet, load
from berth.mirror import newest, plan
from berth.registry import Reply, location, tags
from berth.validate import problems

MIRRORS = """
mirrors:
  redis:
    source: docker.io/library/redis
    target: mirror/redis
    track: "8.2"
"""


@pytest.mark.parametrize(
    ("available", "track", "suffix", "expected"),
    [
        (["8.2.9", "8.2.11", "8.3.0", "8.2.12-rc1", "latest"], "8.2", "", "8.2.11"),  # numeric, not string, order
        (["8.2.11", "8.2.11-alpine", "8.2.12-alpine"], "8.2", "-alpine", "8.2.12-alpine"),
        (["v1.9.0", "v1.10.1", "v2.0.0"], "1", "", "v1.10.1"),
        (["7.4.0"], "8", "", None),
    ],
)
def test_the_newest_tag_on_the_track(available: list[str], track: str, suffix: str, expected: str | None) -> None:
    assert newest(available, track, suffix) == expected


def _fleet(dev_tag: str) -> Fleet:
    cache = app_spec(name="cache", image="mirror/redis", services={"redis": {"port": 6379}})
    other = app_spec(name="other")
    return load(tree({"cache": (cache, {"dev": env_spec(version=f"{dev_tag}@sha256:" + "c" * 64)}), "other": (other, {"dev": env_spec()})}) | {"platform/mirrors.yaml": MIRRORS})


def test_an_app_behind_the_newest_tag_gets_a_dev_release() -> None:
    fleet = _fleet("8.2.9")
    (update,) = plan(fleet, {"redis": ["8.2.9", "8.2.11"]}).updates
    assert (update.tag, update.env, update.apps, update.target) == ("8.2.11", "dev", ("cache",), "mirror/redis")


def test_an_app_already_current_gets_nothing() -> None:
    assert plan(_fleet("8.2.11"), {"redis": ["8.2.9", "8.2.11"]}).updates == ()


def test_a_mirrored_image_needs_a_mirror_entry() -> None:
    cache = app_spec(name="cache", image="mirror/redis", services={"redis": {"port": 6379}})
    found = problems(load(tree({"cache": (cache, {"dev": env_spec()})})))
    assert any("mirror/redis has no entry in platform/mirrors.yaml" in problem for problem in found)


def test_docker_hub_names_map_to_its_api() -> None:
    assert location("docker.io/library/redis") == ("https://registry-1.docker.io", "library/redis")
    assert location("docker.io/redis") == ("https://registry-1.docker.io", "library/redis")
    assert location("ghcr.io/external-secrets/external-secrets") == ("https://ghcr.io", "external-secrets/external-secrets")


def test_tags_follow_the_token_challenge_and_pagination() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    challenge = 'Bearer realm="https://auth.example.test/token",service="registry.example.test",scope="repository:team/app:pull"'

    def get(url: str, headers: Mapping[str, str]) -> Reply:
        sent = dict(headers)
        seen.append((url, sent))
        if url.startswith("https://auth.example.test/token"):
            return Reply(200, {}, json.dumps({"token": "t0k"}).encode())
        if "authorization" not in sent:
            return Reply(401, {"www-authenticate": challenge}, b"")
        if "last=" not in url:
            return Reply(200, {"link": '</v2/team/app/tags/list?n=1000&last=1.0.1>; rel="next"'}, json.dumps({"tags": ["1.0.0", "1.0.1"]}).encode())
        return Reply(200, {}, json.dumps({"tags": ["1.0.2"]}).encode())

    assert tags("registry.example.test/team/app", get) == ["1.0.0", "1.0.1", "1.0.2"]
    assert seen[1][0] == "https://auth.example.test/token?service=registry.example.test&scope=repository%3Ateam%2Fapp%3Apull"
    assert seen[-1] == ("https://registry.example.test/v2/team/app/tags/list?n=1000&last=1.0.1", {"authorization": "Bearer t0k"})
