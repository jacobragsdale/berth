"""End to end through the CLI, in a throwaway git repository built from this repo's platform files and apps."""

import io
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import REPO

from berth.cli import parse, run

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
ALICE, BOB, INFRA = "alice.ng@contoso.com", "bob.osei@contoso.com", "infra.lead@contoso.com"


def berth(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = run(parse(argv), out, NOW)
    return code, out.getvalue()


def git(root: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@contoso.com", *args], check=True, capture_output=True, env=env)  # noqa: S607


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A valid deploy repo (specs and schemas agree) with one commit on main. Nothing rendered is committed."""
    for part in ("platform", "apps/payments-api", "apps/ledger", "apps/customer-portal"):
        shutil.copytree(REPO / part, tmp_path / part)
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def review(repo: Path, *argv: str) -> str:
    return berth("--root", str(repo), "review", *argv)[1]


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_check_passes_on_this_repository() -> None:
    code, out = berth("--root", str(REPO), "check")
    assert (code, out.splitlines()[-1]) == (0, "ok: 7 apps, 20 app environments")


def test_a_change_to_berth_itself_passes_the_gate_with_a_platform_approver(repo: Path) -> None:
    # The gate runs main's berth. It used to compare committed output with what main's renderer produces, so a renderer
    # change could never pass. Nothing rendered is committed now: the change is judged as code, by the platform team.
    (repo / "src/berth/render").mkdir(parents=True)
    (repo / "src/berth/render/k8s.py").write_text("# a renderer change\n")
    pending = review(repo, "--author", INFRA)
    approved = review(repo, "--author", INFRA, "--approve", "platform.oncall@contoso.com")
    assert "platform files: platform review" in pending
    assert "gate: pending" in pending
    assert "gate: succeeded" in approved
    assert "note: this changes berth itself" in approved


def test_rendering_writes_a_tree_git_ignores(repo: Path) -> None:
    code, out = berth("--root", str(repo), "render")
    assert code == 0
    assert "into" in out
    assert (repo / "rendered/k8s/aks-prod/ledger-prod/manifests.yaml").is_file()
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True, check=True).stdout  # noqa: S607
    assert status == ""
    assert "gate: succeeded" in review(repo, "--author", ALICE)  # nothing to review


def test_render_will_not_write_over_a_directory_that_is_not_a_rendered_tree(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "notes.txt").write_text("mine\n")
    code, out = berth("--root", str(repo), "render", "--out", str(other))
    assert code == 1
    assert "not a rendered tree" in out
    assert (other / "notes.txt").read_text() == "mine\n"


def test_diff_shows_what_a_change_does_to_the_rendered_output(repo: Path, tmp_path: Path) -> None:
    (repo / "apps/payments-api/prod.yaml").write_text((repo / "apps/payments-api/prod.yaml").read_text() + "config:\n  LOG_LEVEL: warn\n")
    code, out = berth("--root", str(repo), "diff", "--base", "main", "--out", str(tmp_path / "rendered.diff"))
    assert code == 0
    assert "2 rendered files change against main" in out  # the manifests, and the DAG that names the new ConfigMap
    assert "k8s/aks-prod/payments-api-prod/manifests.yaml" in out
    assert "airflow/prod/payments-api__settlement.py" in out
    assert "+  LOG_LEVEL: warn" in (tmp_path / "rendered.diff").read_text()


def test_a_new_caller_needs_the_called_apps_owners(repo: Path) -> None:
    edit(repo / "apps/customer-portal/app.yaml", "talks_to: [payments-api/api]", "talks_to: [payments-api/api, ledger/api]")
    out = review(repo, "--author", "erin.walsh@contoso.com", "--approve", "farid.haddad@contoso.com")
    assert "ledger: owner review" in out
    assert "callers.customer-portal" in out
    assert "ledger: needs approval from an owner other than the author (carol.diaz@contoso.com, dev.patel@contoso.com)" in out
    approved = review(repo, "--author", "erin.walsh@contoso.com", "--approve", "farid.haddad@contoso.com", "--approve", "carol.diaz@contoso.com")
    assert "gate: succeeded" in approved


def test_a_file_with_a_dot_in_its_name_is_an_application_setting(repo: Path) -> None:
    edit(repo / "apps/payments-api/app.yaml", "secrets: [db-password, card-processor-key]", "files:\n  limits.toml: |\n    max = 1\nsecrets: [db-password, card-processor-key]")
    git(repo, "commit", "-qam", "add a file")
    edit(repo / "apps/payments-api/app.yaml", "max = 1", "max = 2")
    out = review(repo, "--author", ALICE, "--approve", BOB)
    assert "dev   files[limits.toml]  (1 line) -> (1 line)  auto" in out
    assert "no review rule covers this path" not in out


def test_a_production_setting_needs_the_other_owner(repo: Path) -> None:
    (repo / "apps/payments-api/prod.yaml").write_text((repo / "apps/payments-api/prod.yaml").read_text() + "config:\n  LOG_LEVEL: warn\n")
    pending = review(repo, "--author", ALICE)
    approved = review(repo, "--author", ALICE, "--approve", BOB)
    assert "payments-api: owner review" in pending
    assert "gate: pending" in pending
    assert "gate: succeeded" in approved


def test_a_pull_request_cannot_loosen_its_own_review(repo: Path) -> None:
    edit(
        repo / "platform/review.yaml",
        "  - reason: access to Azure resources\n    paths: [azure_identity]\n    tier: platform",
        "  - reason: access to Azure resources\n    paths: [azure_identity]\n    tier: auto",
    )
    edit(repo / "apps/ledger/app.yaml", "azure_identity: true", "azure_identity: false")
    out = review(repo, "--author", "carol.diaz@contoso.com")
    assert "ledger: platform review" in out  # judged by the target branch's policy, not the edited one
    assert "platform files: platform review" in out


def test_promoting_what_ran_in_qa_needs_an_owner_but_skipping_qa_needs_the_platform(repo: Path) -> None:
    code, _ = berth("--root", str(repo), "promote", "ledger", "--to", "prod")
    assert code == 0
    promoted = review(repo, "--author", "carol.diaz@contoso.com")
    assert "ledger: owner review" in promoted

    git(repo, "checkout", "-q", "--", ".")
    dev_version = next(line.split(": ", 1)[1] for line in (repo / "apps/payments-api/dev.yaml").read_text().splitlines() if line.startswith("version:"))
    code, _ = berth("--root", str(repo), "release", "payments-api", "--env", "prod", "--version", dev_version)
    assert code == 0
    skipped = review(repo, "--author", ALICE, "--approve", BOB)
    assert "has not run in qa" in skipped
    assert "payments-api: platform review" in skipped


def test_the_release_bot_merges_dev_versions_alone_and_nothing_else(repo: Path) -> None:
    version = "1.6.0@sha256:" + "b" * 64
    berth("--root", str(repo), "release", "payments-api", "--env", "dev", "--version", version)
    ok = review(repo, "--author", "berth-release@contoso.com")
    edit(repo / "apps/payments-api/dev.yaml", "LOG_LEVEL: debug", "LOG_LEVEL: trace")
    sneaky = review(repo, "--author", "berth-release@contoso.com")
    assert "gate: succeeded" in ok
    assert "gate: failed" in sneaky


def test_release_keeps_comments_and_layout(repo: Path) -> None:
    path = repo / "apps/payments-api/dev.yaml"
    before = path.read_text()
    berth("--root", str(repo), "release", "payments-api", "--env", "dev", "--version", "1.6.0@sha256:" + "c" * 64)
    after = path.read_text()
    assert after.splitlines()[0] == before.splitlines()[0]  # the schema comment survives
    assert [line for line in after.splitlines() if not line.startswith("version:")] == [line for line in before.splitlines() if not line.startswith("version:")]


def test_a_renderer_change_lists_affected_apps_without_asking_their_owners(repo: Path) -> None:
    edit(repo / "platform/platform.yaml", "default_replicas: 2", "default_replicas: 3")
    out = review(repo, "--author", INFRA)
    assert "rendered output changes without a spec change" in out
    assert "customer-portal: prod" not in out  # it pins replicas: 3 in prod, so its output did not change
    assert "ledger: prod" in out
    assert "owner review" not in out


@pytest.mark.integration
@pytest.mark.parametrize("script", ["validate-rendered.sh", "validate-software.sh"])
def test_rendered_output_passes_schemas_policies_and_permissions(script: str) -> None:
    done = subprocess.run([str(REPO / "scripts" / script)], capture_output=True, text=True, check=False, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr


def test_images_lists_only_digests_that_are_new_against_the_base(repo: Path) -> None:
    digest = "sha256:" + "c" * 64
    edit(repo / "apps/ledger/qa.yaml", "version: ", f"version: 9.9.9@{digest}\n# was: ")
    code, out = berth("--root", str(repo), "images", "--base", "main")
    assert (code, out) == (0, f"contosoacr.azurecr.io/ledger@{digest}\t9.9.9\n")


def test_explain_shows_where_to_look_and_what_is_rendered(repo: Path) -> None:
    code, out = berth("--root", str(repo), "explain", "ledger", "--env", "prod")
    assert code == 0
    assert "kubectl --context aks-prod -n ledger-prod get pods" in out
    assert "  Deployment ledger-api" in out
