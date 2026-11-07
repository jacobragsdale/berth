#!/usr/bin/env bash
# Render each platform software chart with the values berth would deploy, and validate the result against
# Kubernetes and CRD schemas. Downloads charts, so it needs network access. The output is not committed (the CRDs
# alone are megabytes); the pull request pipeline attaches the rendered diff of a chart change instead.
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/fetch-tools.sh >/dev/null
BIN=.tools/bin
export HELM_CACHE_HOME="$PWD/.tools/helm/cache" HELM_CONFIG_HOME="$PWD/.tools/helm/config" HELM_DATA_HOME="$PWD/.tools/helm/data"
OUT=${1:-$(mktemp -d)}
CACHE=.tools/kubeconform-cache
mkdir -p "$CACHE"
CRDS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

uv run python scripts/template_software.py "$BIN/helm" "$OUT"
# CustomResourceDefinitions are skipped: kubeconform has no schema for the CRD objects themselves.
"$BIN/kubeconform" -strict -summary -cache "$CACHE" -skip CustomResourceDefinition -kubernetes-version 1.33.0 \
  -schema-location default -schema-location "$CRDS" "$OUT"
