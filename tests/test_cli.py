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
    """A valid deploy repo (specs, schemas and rendered output agree) with one commit on main."""
    for part in ("platform", "apps/payments-api", "apps/ledger", "apps/customer-portal"):
        shutil.copytree(REPO / part, tmp_path / part)
    git(tmp_path, "init", "-q", "-b", "main")
    assert berth("--root", str(tmp_path), "render")[0] == 0
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def review(repo: Path, *argv: str) -> str:
    """Render, as the author would before pushing, then review against main."""
    assert berth("--root", str(repo), "render")[0] == 0
    return berth("--root", str(repo), "review", *argv)[1]


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_check_passes_on_this_repository() -> None:
    code, out = berth("--root", str(REPO), "check")
    assert (code, out.splitlines()[-1]) == (0, "ok: 7 apps, 20 app environments")


def test_hand_edited_rendered_output_fails_the_gate(repo: Path) -> None:
    edit(repo / "rendered/k8s/aks-prod/ledger-prod/manifests.yaml", "runAsNonRoot: true", "runAsNonRoot: false")
    _, out = berth("--root", str(repo), "review", "--author", INFRA, "--approve", "platform.oncall@contoso.com")
    assert "gate: failed" in out
    assert "rendered/k8s/aks-prod/ledger-prod/manifests.yaml: stale" in out


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
