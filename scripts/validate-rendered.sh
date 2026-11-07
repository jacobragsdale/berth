#!/usr/bin/env bash
# Validate everything berth renders, the way the cluster will: Kubernetes and CRD schemas (kubeconform),
# then the admission policies (Kyverno), including the pods Airflow and Control-M start. Then prove each
# policy still rejects what it should (kyverno test).
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/fetch-tools.sh >/dev/null
BIN=.tools/bin
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

uv run python -I scripts/extract_workloads.py rendered "$WORK/scheduled"

"$BIN/kubeconform" -strict -summary -kubernetes-version 1.33.0 \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  rendered/k8s "$WORK/scheduled"

find rendered/k8s -name manifests.yaml -exec cat {} + > "$WORK/all.yaml"
for f in "$WORK"/scheduled/*.yaml; do printf -- '---\n' >> "$WORK/all.yaml"; cat "$f" >> "$WORK/all.yaml"; done
result=$("$BIN/kyverno" apply platform/admission/*.yaml --resource "$WORK/all.yaml" 2>&1 | grep -v '^Warning:' )
echo "$result" | tail -1
if ! echo "$result" | grep -q 'fail: 0, warn: 0, error: 0'; then
  echo "$result"
  echo "admission policies reject rendered output" >&2
  exit 1
fi

"$BIN/kyverno" test platform/admission/tests | grep "Test Summary"
