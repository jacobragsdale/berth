"""Builders for small repository trees. Tests use the repo's real platform.yaml and review.yaml: those files are
the policy under test, so a test that says "a prod config change needs an owner" checks the table people edit."""

from collections.abc import Mapping
from pathlib import Path

from berth import yamlio
from berth.fleet import Fleet, load

REPO = Path(__file__).resolve().parents[1]
PLATFORM_TEXT = (REPO / "platform" / "platform.yaml").read_text()
POLICY_TEXT = (REPO / "platform" / "review.yaml").read_text()
DIGEST = "sha256:" + "a" * 64
OWNERS = ("ann@contoso.com", "ben@contoso.com")
PLATFORM_APPROVER = "infra.lead@contoso.com"
OTHER_PLATFORM_APPROVER = "platform.oncall@contoso.com"
RELEASE_BOT = "berth-release@contoso.com"


def version(tag: str) -> str:
    return f"{tag}@{DIGEST}"


def app_spec(**fields: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": "demo",
        "description": "Demo app",
        "owners": list(OWNERS),
        "source": "https://dev.azure.com/contoso/demo/_git/demo",
        "services": {"api": {"port": 8080, "size": "s"}},
    }
    return spec | fields


def env_spec(**fields: object) -> dict[str, object]:
    return {"version": version("1.0.0")} | fields


def tree(apps: Mapping[str, tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]] | None = None) -> dict[str, str]:
    """A repository with the real platform files and the given apps: {name: (app.yaml, {env: env.yaml})}."""
    files = {"platform/platform.yaml": PLATFORM_TEXT, "platform/review.yaml": POLICY_TEXT}
    chosen = apps if apps is not None else {"demo": (app_spec(), {"dev": env_spec(), "qa": env_spec(), "prod": env_spec()})}
    for name, (spec, envs) in chosen.items():
        files[f"apps/{name}/app.yaml"] = yamlio.dump(spec)
        for env, body in envs.items():
            files[f"apps/{name}/{env}.yaml"] = yamlio.dump(body)
    return files


def fleet(apps: Mapping[str, tuple[Mapping[str, object], Mapping[str, Mapping[str, object]]]] | None = None) -> Fleet:
    return load(tree(apps))


def dig(value: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(value, dict), f"expected a mapping at {key!r}"
        value = {str(name): item for name, item in value.items()}[key]
    return value


def items(value: object) -> list[object]:
    assert isinstance(value, list)
    return list(value)
