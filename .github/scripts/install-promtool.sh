#!/usr/bin/env bash
# Install promtool (from the official Prometheus release, checked by SHA-256) into
# /usr/local/bin, for the metrics checks in CI.
set -euo pipefail

readonly VERSION="3.16.0"
readonly SHA256="68727009472a2c02e1ff7f062fd5b3e637285bffc8fc2d340c76fb449d6e0f34"
readonly NAME="prometheus-${VERSION}.linux-amd64"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
curl -fsSL --retry 3 -o "${work}/${NAME}.tar.gz" \
  "https://github.com/prometheus/prometheus/releases/download/v${VERSION}/${NAME}.tar.gz"
echo "${SHA256}  ${work}/${NAME}.tar.gz" | sha256sum --check --strict --quiet
tar -xzf "${work}/${NAME}.tar.gz" -C "${work}" "${NAME}/promtool"
sudo install -m 0755 "${work}/${NAME}/promtool" /usr/local/bin/promtool
promtool --version | head -n1
