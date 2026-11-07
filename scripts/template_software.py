"""Render every platform software release with `helm template`, exactly as `berth deploy` would, into OUT_DIR.

Usage: python scripts/template_software.py HELM OUT_DIR [KUBE_VERSION]

Writes OUT_DIR/<cluster>/<release>.yaml. Downloads the charts, so it needs network access.
"""

import sys
from pathlib import Path

from berth import gitio
from berth.fleet import load
from berth.kube import helm_template
from berth.render.software import releases, template_args, values


def main() -> int:
    helm, out = sys.argv[1], Path(sys.argv[2])
    kube_version = sys.argv[3] if len(sys.argv) > 3 else "1.33.0"  # noqa: PLR2004 - an optional third argument
    fleet = load(gitio.worktree(Path()))
    count = 0
    for cluster in sorted(fleet.platform.clusters):
        for release in releases(fleet.software, cluster):
            target = out / cluster / f"{release.name}.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(helm_template(helm, template_args(release, kube_version), values(release, fleet.platform)))
            print(f"templated {cluster}/{release.name}")
            count += 1
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
