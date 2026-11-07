#!/usr/bin/env bash
# Download pinned, checksum-verified copies of the validation tools into .tools/bin.
# CI runs this too, so local checks and pipeline checks use the same binaries.
set -euo pipefail
cd "$(dirname "$0")/.."
KUBECONFORM=v0.8.0
HELM=v4.3.1
ENVTEST=v1.34.1  # kube-apiserver, etcd and kubectl, for tests against a real API server
BIN=.tools/bin
mkdir -p "$BIN" .tools/dl

verify() { # file sums-url asset [sha256sum|sha512sum]
  curl -fsSL --max-time 30 "$2" | grep -E "[ *]$3\$" | sed -E "s|[ *]+$3\$|  $1|" | "${4:-sha256sum}" --check --quiet
}

fetch_tar() { # url sums-url asset member binary
  local url=$1 sums=$2 asset=$3 member=$4 binary=$5
  [[ -x "$BIN/$binary" ]] && return
  curl -fsSL --max-time 120 -o ".tools/dl/$asset" "$url"
  verify ".tools/dl/$asset" "$sums" "$asset"
  tar -xzf ".tools/dl/$asset" -C .tools/dl "$member"
  install -m 0755 ".tools/dl/$member" "$BIN/$binary"
}

fetch_envtest() {
  local asset="envtest-$ENVTEST-linux-amd64.tar.gz" base="https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-$ENVTEST"
  [[ -x "$BIN/kube-apiserver" ]] && return
  curl -fsSL --max-time 300 -o ".tools/dl/$asset" "$base/$asset"
  # The checksum file holds the bare digest, so build the line sha512sum expects.
  printf '%s  %s\n' "$(curl -fsSL --max-time 30 "$base/$asset.sha512" | awk '{print $1}')" ".tools/dl/$asset" | sha512sum --check --quiet
  tar -xzf ".tools/dl/$asset" -C .tools/dl
  for binary in kube-apiserver etcd kubectl; do install -m 0755 ".tools/dl/controller-tools/envtest/$binary" "$BIN/$binary"; done
}

fetch_tar "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/kubeconform-linux-amd64.tar.gz" \
  "https://github.com/yannh/kubeconform/releases/download/$KUBECONFORM/CHECKSUMS" \
  kubeconform-linux-amd64.tar.gz kubeconform kubeconform
fetch_tar "https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz" \
  "https://get.helm.sh/helm-$HELM-linux-amd64.tar.gz.sha256sum" \
  "helm-$HELM-linux-amd64.tar.gz" linux-amd64/helm helm
fetch_envtest
"$BIN/kubeconform" -v
"$BIN/helm" version --short
"$BIN/kubectl" version --client | sed -n 1p
"$BIN/kube-apiserver" --version
