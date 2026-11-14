"""`berth publish`: the rendered branch shows what each cluster runs, cluster by cluster, namespace by namespace."""

import os
import subprocess
from pathlib import Path

import pytest
from support import app_spec, env_spec, fleet

from berth import gitio
from berth.operate import Outcome, Results, State
from berth.publish import Branch, Deployed, publish, published
from berth.render import render

ENVS = {"dev": env_spec(), "qa": env_spec(), "prod": env_spec()}
NIGHTLY = {"nightly": {"orchestrator": "airflow", "schedule": "0 2 * * *", "tasks": {"run": {}}}}
FLEET = fleet({"demo": (app_spec(workflows=NIGHTLY), ENVS), "other": (app_spec(name="other"), ENVS)})
FILES = render(FLEET)


def _results(platform: str = "deployed", **states: State) -> Results:
    """Outcomes for aks-qa (dev and qa) by namespace; any namespace not named is unchanged."""
    namespaces = {f"{app}-{env}": Outcome(app=app, env=env, state=State.UNCHANGED) for app in ("demo", "other") for env in ("dev", "qa")}
    for namespace, state in states.items():
        app, _, env = namespace.replace("_", "-").rpartition("-")
        namespaces[f"{app}-{env}"] = Outcome(app=app, env=env, state=state)
    return Results.model_validate({"cluster": "aks-qa", "commit": "c" * 40, "platform": platform, "namespaces": namespaces})


def _old(files: dict[str, str]) -> dict[str, str]:
    return {path: text + "# old\n" for path, text in files.items()}


def test_a_cluster_replaces_only_its_own_part() -> None:
    previous = _old(FILES)
    tree = published(FLEET, "aks-qa", FILES, previous, _results())
    assert tree["k8s/aks-qa/demo-dev/manifests.yaml"] == FILES["k8s/aks-qa/demo-dev/manifests.yaml"]
    assert tree["airflow/qa/demo__nightly.py"] == FILES["airflow/qa/demo__nightly.py"]
    assert tree["k8s/aks-prod/demo-prod/manifests.yaml"] == previous["k8s/aks-prod/demo-prod/manifests.yaml"]  # prod's stage publishes prod
    assert tree["airflow/prod/demo__nightly.py"] == previous["airflow/prod/demo__nightly.py"]


def test_an_app_environment_that_did_not_deploy_keeps_what_it_runs() -> None:
    previous = _old(FILES)
    tree = published(FLEET, "aks-qa", FILES, previous, _results(demo_dev=State.FAILED, demo_qa=State.HELD))
    for path in ("k8s/aks-qa/demo-dev/manifests.yaml", "airflow/dev/demo__nightly.py", "airflow/qa/demo__nightly.py"):
        assert tree[path] == previous[path], path  # its schedulers keep starting the pods its namespace can run
    assert tree["k8s/aks-qa/other-dev/manifests.yaml"] == FILES["k8s/aks-qa/other-dev/manifests.yaml"]


def test_a_full_deploy_drops_an_app_that_is_gone_and_a_partial_one_does_not() -> None:
    previous = _old(FILES) | {"k8s/aks-qa/retired-dev/manifests.yaml": "x\n", "airflow/dev/retired__job.py": "x\n"}
    full = published(FLEET, "aks-qa", FILES, previous, _results())
    assert "airflow/dev/retired__job.py" not in full
    one_app = published(FLEET, "aks-qa", FILES, previous, _results(platform="skipped"))
    assert one_app["airflow/dev/retired__job.py"] == "x\n"  # a deploy of one app says nothing about the others


def _git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@contoso.com", *args], check=True, capture_output=True, text=True, env=env)  # noqa: S607
    return done.stdout


def test_publishing_commits_onto_the_branch_without_touching_the_checkout(tmp_path: Path) -> None:
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    (clone / "main.txt").write_text("main\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-qm", "main")
    branch = Branch(clone, "origin")

    first = publish(branch, Deployed(FLEET, "aks-qa", FILES, _results(), "c" * 40))
    assert first is not None
    assert _git(clone, "status", "--porcelain") == ""  # the checkout and its index are as they were
    on_branch = gitio.at_ref(remote, "rendered")
    assert on_branch["k8s/aks-qa/demo-dev/manifests.yaml"] == FILES["k8s/aks-qa/demo-dev/manifests.yaml"]
    assert "k8s/aks-prod/demo-prod/manifests.yaml" not in on_branch

    assert publish(branch, Deployed(FLEET, "aks-qa", FILES, _results(), "c" * 40)) is None  # nothing changed
    second = publish(branch, Deployed(FLEET, "aks-prod", FILES, None, "c" * 40))
    assert second is not None
    after = gitio.at_ref(remote, "rendered")
    assert after["k8s/aks-prod/demo-prod/manifests.yaml"] == FILES["k8s/aks-prod/demo-prod/manifests.yaml"]
    assert after["k8s/aks-qa/demo-dev/manifests.yaml"] == FILES["k8s/aks-qa/demo-dev/manifests.yaml"]
    assert _git(remote, "log", "--format=%s", "rendered").splitlines() == [f"aks-prod: rendered from {'c' * 12}", f"aks-qa: rendered from {'c' * 12} (unchanged)"]


def test_a_publish_that_loses_a_race_keeps_what_the_other_cluster_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    branch = Branch(clone, "origin")
    assert publish(branch, Deployed(FLEET, "aks-qa", _old(FILES), _results(), "b" * 40)) is not None
    push, pushes = gitio.push, []

    def prod_publishes_first(root: Path, remote_name: str, commit: str, name: str) -> None:
        """Another run's prod stage publishes between this run reading the branch and pushing to it."""
        pushes.append(commit)
        if len(pushes) == 1:
            monkeypatch.setattr(gitio, "push", push)
            assert publish(branch, Deployed(FLEET, "aks-prod", FILES, None, "d" * 40)) is not None
            monkeypatch.setattr(gitio, "push", prod_publishes_first)
        push(root, remote_name, commit, name)

    monkeypatch.setattr(gitio, "push", prod_publishes_first)
    assert publish(branch, Deployed(FLEET, "aks-qa", FILES, _results(), "c" * 40)) == pushes[-1]
    assert len(pushes) == 2  # refused once, then on top of what prod published
    after = gitio.at_ref(remote, "rendered")
    assert after["k8s/aks-qa/demo-dev/manifests.yaml"] == FILES["k8s/aks-qa/demo-dev/manifests.yaml"]
    assert after["k8s/aks-prod/demo-prod/manifests.yaml"] == FILES["k8s/aks-prod/demo-prod/manifests.yaml"]


def test_a_push_refused_for_another_reason_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    remote, clone = tmp_path / "remote.git", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    attempts = []

    def refuse(_root: Path, _remote: str, commit: str, _name: str) -> None:
        attempts.append(commit)
        msg = "git push failed: you may not push to rendered"
        raise gitio.GitError(msg)

    monkeypatch.setattr(gitio, "push", refuse)
    with pytest.raises(gitio.GitError, match="may not push"):
        publish(Branch(clone, "origin"), Deployed(FLEET, "aks-qa", FILES, _results(), "c" * 40))
    assert len(attempts) == 1
