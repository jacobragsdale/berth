#!/usr/bin/env bash
# Validate everything berth renders against Kubernetes and CRD schemas (kubeconform), including the pods Airflow
# and Control-M start, for the clusters' version and the next one, so an API the next upgrade removes fails here
# first. Runs offline once tools and schemas are cached. The admission policies are tested against a real API
# server by `uv run pytest -m integration` (tests/test_cluster.py).
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/fetch-tools.sh >/dev/null
BIN=.tools/bin
KUBERNETES_VERSIONS="1.33.0 1.34.0"  # the clusters' version, then the next upgrade
CACHE=.tools/kubeconform-cache  # schemas are downloaded once, then read from here
mkdir -p "$CACHE"
CRDS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

uv run python -I scripts/extract_workloads.py rendered "$WORK/scheduled"

for version in $KUBERNETES_VERSIONS; do
  echo "Kubernetes $version:"
  "$BIN/kubeconform" -strict -summary -cache "$CACHE" -kubernetes-version "$version" -schema-location default -schema-location "$CRDS" \
    rendered/k8s "$WORK/scheduled"
done
