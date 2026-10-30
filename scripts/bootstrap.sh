#!/usr/bin/env bash
# Prepare an Ubuntu server for drkit: Docker, restic, git, Ansible, certbot and drkit itself.
#
#   curl -fsSL https://raw.githubusercontent.com/caiodassumpcao/drkit/main/scripts/bootstrap.sh | sudo bash
#
# Idempotent: run it again at any time; what is already in place is kept, and drkit is
# (re)installed at the requested version. Downloads are pinned and checked by SHA-256.
#
# Environment:
#   DRKIT_SPEC                what to install as drkit: a pip requirement or a local checkout
#                             (default: the release this script belongs to)
#   DRKIT_ALLOW_UNSUPPORTED=1 run on a release other than Ubuntu 22.04 / 24.04
set -euo pipefail

readonly DRKIT_VERSION="0.4.0"
readonly DRKIT_SPEC="${DRKIT_SPEC:-git+https://github.com/caiodassumpcao/drkit.git@v${DRKIT_VERSION}}"
readonly RESTIC_VERSION="0.18.1"
readonly UV_VERSION="0.13.6"
readonly PYTHON_VERSION="3.12"
readonly ANSIBLE_SPEC="ansible-core>=2.17,<2.22"
readonly SUPPORTED_RELEASES="22.04 24.04"

# uv keeps its Python, drkit and Ansible under /opt/drkit; the commands land in /usr/local/bin.
export UV_PYTHON_INSTALL_DIR=/opt/drkit/python
export UV_TOOL_DIR=/opt/drkit/tools
export UV_TOOL_BIN_DIR=/usr/local/bin
export DEBIAN_FRONTEND=noninteractive

log() { printf '==> %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

# Checksums of the official release files (from each release's SHA256SUMS / .sha256 files).
restic_sha256() {
  case "$1" in
    amd64) echo "680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d" ;;
    arm64) echo "87f53fddde38764095e9c058a3b31834052c37e5826d2acf34e18923c006bd45" ;;
  esac
}
uv_sha256() {
  case "$1" in
    amd64) echo "f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645" ;;
    arm64) echo "229e194255c7958d86d56e8790d969a6f74736592133f0dfd0076f754c642c1d" ;;
  esac
}

check_system() {
  [[ ${EUID} -eq 0 ]] || die "run as root (sudo bash $0)"
  # shellcheck source=/dev/null
  . /etc/os-release
  if [[ ${ID:-} != ubuntu || " ${SUPPORTED_RELEASES} " != *" ${VERSION_ID:-} "* ]]; then
    [[ ${DRKIT_ALLOW_UNSUPPORTED:-0} == 1 ]] ||
      die "${PRETTY_NAME:-this system} is not Ubuntu ${SUPPORTED_RELEASES// / or }" \
        "(set DRKIT_ALLOW_UNSUPPORTED=1 to try anyway)"
    log "warning: ${PRETTY_NAME:-unknown system} is not supported"
  fi
  ARCH="$(dpkg --print-architecture)"
  [[ ${ARCH} == amd64 || ${ARCH} == arm64 ]] || die "unsupported architecture: ${ARCH}"
}

# download <url> <sha256> <file>
download() {
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum --check --strict --quiet || die "checksum mismatch for $1"
}

install_packages() {
  log "Installing base packages"
  apt-get update -qq </dev/null
  apt-get install -y -qq ca-certificates curl git bzip2 gnupg python3-apt certbot </dev/null
}

install_docker() {
  if docker version >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "Docker $(docker version --format '{{.Server.Version}}') already installed"
    return
  fi
  log "Installing Docker Engine and the Compose plugin (download.docker.com)"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck source=/dev/null
  echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -qq </dev/null
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin </dev/null
  if [[ -d /run/systemd/system ]]; then
    systemctl enable --now docker
  else
    log "warning: systemd is not running; start the Docker daemon yourself"
  fi
}

install_restic() {
  if command -v restic >/dev/null && [[ "$(restic version)" == "restic ${RESTIC_VERSION} "* ]]; then
    log "restic ${RESTIC_VERSION} already installed"
    return
  fi
  log "Installing restic ${RESTIC_VERSION}"
  download "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${ARCH}.bz2" \
    "$(restic_sha256 "${ARCH}")" "${WORK}/restic.bz2"
  bunzip2 "${WORK}/restic.bz2"
  install -m 0755 "${WORK}/restic" /usr/local/bin/restic
}

install_uv() {
  if command -v uv >/dev/null && [[ "$(uv --version)" == "uv ${UV_VERSION}"* ]]; then
    log "uv ${UV_VERSION} already installed"
    return
  fi
  local target
  target="$([[ ${ARCH} == amd64 ]] && echo x86_64 || echo aarch64)-unknown-linux-gnu"
  log "Installing uv ${UV_VERSION}"
  download "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}.tar.gz" \
    "$(uv_sha256 "${ARCH}")" "${WORK}/uv.tar.gz"
  tar -xzf "${WORK}/uv.tar.gz" -C "${WORK}"
  install -m 0755 "${WORK}/uv-${target}/uv" "${WORK}/uv-${target}/uvx" /usr/local/bin/
}

install_tools() {
  log "Installing Python ${PYTHON_VERSION}, Ansible (${ANSIBLE_SPEC}) and drkit (${DRKIT_SPEC})"
  uv python install --quiet "${PYTHON_VERSION}"
  uv tool install --quiet --python "${PYTHON_VERSION}" "${ANSIBLE_SPEC}"
  # --force: always the requested drkit, so the same command upgrades or rolls back.
  uv tool install --quiet --force --python "${PYTHON_VERSION}" "${DRKIT_SPEC}"
}

prepare_config() {
  install -d -m 0700 /etc/drkit
}

summary() {
  log "Ready: $(drkit --version), $(restic version | cut -d' ' -f1-2), $(docker --version)," \
    "$(git --version), $(ansible-playbook --version | head -n1)"
  cat >&2 <<'EOF'

Next steps (docs/runbook.md, "Restore a host"):
  1. Put dr.yaml, restic.pass and env back in /etc/drkit (from your password manager),
     owned by root with mode 0600.
  2. sudo drkit doctor
  3. sudo drkit restore host --dry-run
  4. sudo drkit restore host
EOF
}

main() {
  check_system
  WORK="$(mktemp -d)"
  trap 'rm -rf "${WORK}"' EXIT
  install_packages
  install_docker
  install_restic
  install_uv
  install_tools
  prepare_config
  summary
}

# Everything runs from here, so a truncated download (curl | bash) never runs half a script.
main "$@"
