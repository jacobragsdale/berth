#!/usr/bin/env bash
# Download pinned, checksum-verified copies of kubeconform and the Kyverno CLI into .tools/bin.
# CI runs this too, so local checks and pipeline checks use the same binaries.
set -euo pipefail
cd "$(dirname "$0")/.."
KUBECONFORM=v0.8.0
KYVERNO=v1.19.2
BIN=.tools/bin
mkdir -p "$BIN" .tools/dl

fetch() { # url sums-url asset binary
  local url=$1 sums=$2 asset=$3 binary=$4
  [[ -x "$BIN/$binary" ]] && return
  curl -fsSL --max-time 120 -o ".tools/dl/$asset" "$url"
  curl -fsSL --max-time 30 "$sums" | grep " $asset\$" | sed "s| $asset\$| .tools/dl/$asset|" | sha256sum --check --quiet
  tar -xzf ".tools/dl/$asset" -C .tools/dl "$binary"
  install -m 0755 ".tools/dl/$binary" "$BIN/$binary"
}

fetch "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/kubeconform-linux-amd64.tar.gz" \
  "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/CHECKSUMS" kubeconform-linux-amd64.tar.gz kubeconform
fetch "https://github.com/kyverno/kyverno/releases/download/$KYVERNO/kyverno-cli_${KYVERNO}_linux_x86_64.tar.gz" \
  "https://github.com/kyverno/kyverno/releases/download/$KYVERNO/checksums.txt" "kyverno-cli_${KYVERNO}_linux_x86_64.tar.gz" kyverno
"$BIN/kubeconform" -v
"$BIN/kyverno" version | sed -n 1p
