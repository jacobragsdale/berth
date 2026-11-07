"""Write the pods and jobs embedded in rendered Airflow DAGs and Control-M folders out as plain manifests.

Usage: python -I scripts/extract_workloads.py RENDERED_DIR OUT_DIR

Those pods reach the cluster through a scheduler rather than `berth deploy`, so CI validates them separately with
the same schemas, and the integration tests submit them to a real API server to prove the admission policies accept
them. Reads the files as data (ast.literal_eval and JSON); it never imports a DAG.
"""

import ast
import json
import sys
from pathlib import Path

from ruamel.yaml import YAML


def _mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{what}: expected a mapping"
        raise TypeError(msg)
    return {str(key): item for key, item in value.items()}


def _write(out: Path, stem: str, manifest: object) -> None:
    doc = _mapping(manifest, stem)
    metadata = _mapping(doc.get("metadata", {}), f"{stem} metadata")
    # The scheduler names these at run time; the offline validators need a name, so borrow the file's.
    metadata.pop("generateName", None)
    metadata.setdefault("name", stem)
    doc["metadata"] = metadata
    (out / f"{stem}.yaml").write_text(json.dumps(doc, indent=2) + "\n")


def _yaml(text: object) -> object:
    data: object = YAML(typ="safe", pure=True).load(str(text))
    return data


def main(rendered: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for dag in sorted(rendered.glob("airflow/*/*.py")):
        tree = ast.parse(dag.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id.endswith("_POD"):
                pod: object = ast.literal_eval(node.value)
                _write(out, f"airflow-{dag.parent.name}-{dag.stem.replace('_', '-')}-{node.targets[0].id.lower().replace('_', '-')}", pod)
                count += 1
    for definitions in sorted(rendered.glob("controlm/*/*.json")):
        folders = _mapping(json.loads(definitions.read_text()), str(definitions))
        for folder_name, folder in folders.items():
            for job_name, raw in _mapping(folder, folder_name).items():
                if not isinstance(raw, dict):
                    continue  # folder attributes such as "Type" and "Application"
                job = _mapping(raw, job_name)
                if job.get("Type") == "Job:Kubernetes":
                    _write(out, f"controlm-{definitions.parent.name}-{folder_name}-{job_name}", _yaml(job["Job Spec Yaml"]))
                    count += 1
    print(f"extracted {count} scheduler workloads to {out}")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
