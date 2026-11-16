#!/usr/bin/env bash
# Standalone public bootstrap. Runtime data/configuration never live in the checkout.
set -Eeuo pipefail
umask 077
REPOSITORY='https://github.com/HummaX/DriveTheater.git'
APP_VERSION=main
INSTALL_DIR=/opt/drivetheater
DATA_DIR=/var/lib/drivetheater
ENV_DIR=/etc/drivetheater
PORT=80
PUBLIC_URL=''
SETUP_BUNDLE=''
fail() { printf 'DriveTheater: %s\n' "$*" >&2; exit 1; }
usage() {
    echo 'Usage: install.sh [--version COMMIT_OR_TAG] [--port 80] [--data-dir /var/lib/drivetheater] [--install-dir /opt/drivetheater] [--public-url https://media.example.com] [--setup-bundle /absolute/private.json]'
    echo 'Fresh Ubuntu 24.04 setup. Existing installations are preserved; use manage.sh update for updates.'
}
# os-release defines VERSION (the Ubuntu display name), not an application revision.
# Read it in a subshell so it cannot overwrite any bootstrap variables.
check_ubuntu_release() (
    local release_file=${1:-/etc/os-release}
    [[ -r "$release_file" ]] || fail 'Cannot identify the operating system'
    . "$release_file"
    [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || fail 'This installer targets Ubuntu 24.04'
)
main() {
while (( $# )); do
    case "$1" in
        --version|--port|--data-dir|--install-dir|--public-url|--setup-bundle)
            (( $# >= 2 )) || fail "Missing value for $1"
            case "$1" in
                --version) APP_VERSION=$2;; --port) PORT=$2;; --data-dir) DATA_DIR=$2;;
                --install-dir) INSTALL_DIR=$2;; --public-url) PUBLIC_URL=$2;; --setup-bundle) SETUP_BUNDLE=$2;;
            esac
            shift 2;;
        -h|--help) usage; exit 0;;
        *) fail "Unknown argument: $1";;
    esac
done
[[ "$APP_VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$APP_VERSION" != *..* ]] || fail 'Invalid version'
[[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || fail 'Port must be 1..65535'
[[ "$INSTALL_DIR" == /* && "$DATA_DIR" == /* ]] || fail 'Use absolute install and data paths'
# A piped bootstrap has no reusable source filename. Re-download the same selected
# version to a private temporary file before sudo; never run a different branch.
if (( EUID != 0 )); then
    command -v sudo >/dev/null || fail 'Run as root, or install sudo for this user'
    TEMP_SCRIPT=$(mktemp)
    trap 'rm -f -- "$TEMP_SCRIPT"' EXIT
    if [[ -f "${BASH_SOURCE[0]:-}" && ! "${BASH_SOURCE[0]}" == /dev/* ]]; then
        cp -- "${BASH_SOURCE[0]}" "$TEMP_SCRIPT"
    else
        curl --proto '=https' --tlsv1.2 -fsSL "https://raw.githubusercontent.com/HummaX/DriveTheater/$APP_VERSION/scripts/install.sh" -o "$TEMP_SCRIPT"
    fi
    if [[ -n "$SETUP_BUNDLE" ]]; then
        sudo bash "$TEMP_SCRIPT" --version "$APP_VERSION" --port "$PORT" --data-dir "$DATA_DIR" --install-dir "$INSTALL_DIR" --public-url "$PUBLIC_URL" --setup-bundle "$SETUP_BUNDLE"
    else
        sudo bash "$TEMP_SCRIPT" --version "$APP_VERSION" --port "$PORT" --data-dir "$DATA_DIR" --install-dir "$INSTALL_DIR" --public-url "$PUBLIC_URL"
    fi
    exit $?
fi
check_ubuntu_release
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) fail 'Supported architectures: x86_64 and arm64';; esac
command -v flock >/dev/null || fail 'Install util-linux (flock) before continuing'
exec 9>/var/lock/drivetheater-install.lock
flock -n 9 || fail 'Another DriveTheater install/update is running'
for path in "$INSTALL_DIR" "$DATA_DIR" "$ENV_DIR"; do
    [[ "$path" != / && "$path" != *[$'\t\r\n $`\"\047\\:']* ]] || fail 'Use simple absolute paths without whitespace or shell characters'
    [[ "$path" == "$(realpath -m -- "$path")" ]] || fail 'Paths may not contain symlink or dot components'
done
case "$DATA_DIR/" in "$INSTALL_DIR/"*) fail 'Data must be outside the source checkout';; esac
case "$INSTALL_DIR/" in "$DATA_DIR/"*) fail 'Source checkout must be outside data';; esac
if [[ -n "$SETUP_BUNDLE" ]]; then
    [[ "$SETUP_BUNDLE" == /* && -f "$SETUP_BUNDLE" && ! -L "$SETUP_BUNDLE" ]] || fail 'Setup bundle must be a local absolute regular file'
    [[ "$(realpath -- "$SETUP_BUNDLE")" == "$SETUP_BUNDLE" ]] || fail 'Setup bundle may not be a symlink'
fi
packages=()
for pair in 'git:git' 'curl:curl' 'python3:python3'; do
    binary=${pair%%:*}; package=${pair#*:}
    command -v "$binary" >/dev/null || packages+=("$package")
done
if ! command -v docker >/dev/null; then packages+=(docker.io docker-compose-v2); fi
if command -v docker >/dev/null && ! docker compose version >/dev/null 2>&1; then packages+=(docker-compose-v2); fi
if (( ${#packages[@]} )); then
    echo 'Installing missing host prerequisites...'
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates "${packages[@]}"
fi
systemctl start docker
docker info >/dev/null || fail 'Docker daemon is not available'
docker compose version >/dev/null || fail 'Docker Compose v2 is required'
ENV_FILE="$ENV_DIR/install.env"
FRESH=0
if [[ ! -e "$ENV_FILE" && ! -L "$ENV_FILE" ]]; then
    FRESH=1
    python3 - "$PORT" <<'PY'
import socket, sys
try:
    with socket.socket() as sock:
        sock.bind(('0.0.0.0', int(sys.argv[1])))
except OSError:
    sys.exit('Selected port is already in use. Retry with --port 8080; no service was stopped.')
PY
fi
if (( ! FRESH )) && [[ ! -d "$INSTALL_DIR/.git" ]]; then
    fail 'Private installation data exists but its source checkout is missing. Restore the original pinned checkout; refusing to deploy main against existing data.'
fi
if [[ -d "$INSTALL_DIR/.git" && ! -L "$INSTALL_DIR" ]]; then
    [[ "$(git -C "$INSTALL_DIR" config --get remote.origin.url)" == "$REPOSITORY" ]] || fail 'Existing checkout is not DriveTheater; no changes made'
    [[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=all)" ]] || fail 'Existing checkout has local changes; preserve/review them first'
else
    [[ ! -e "$INSTALL_DIR" || ( -d "$INSTALL_DIR" && -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ) ]] || fail 'Install directory is not empty; choose another --install-dir'
    git clone --single-branch "$REPOSITORY" "$INSTALL_DIR"
fi
if (( FRESH )); then
    git -C "$INSTALL_DIR" -c core.hooksPath=/dev/null fetch --no-tags origin "$APP_VERSION"
    [[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=all)" ]] || fail 'Checkout changed; no checkout attempted'
    git -C "$INSTALL_DIR" -c core.hooksPath=/dev/null checkout --detach --no-overwrite-ignore FETCH_HEAD
fi
CONFIG="$INSTALL_DIR/scripts/bootstrap_config.py"
[[ -f "$CONFIG" ]] || fail 'Selected version does not contain the installer configuration helper'
if ! id drivetheater >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin drivetheater
fi
RUNTIME_UID=$(id -u drivetheater); RUNTIME_GID=$(id -g drivetheater)
if [[ -n "$SETUP_BUNDLE" && "$FRESH" != 1 ]]; then
    fail 'Setup bundles are only accepted for a fresh installation; existing configuration is unchanged'
fi
if (( FRESH )); then
    install -d -m 700 "$ENV_DIR"
    echo '1. Creating new private configuration...'
    python3 -I "$CONFIG" init --env-file "$ENV_FILE" --data-dir "$DATA_DIR" --uid "$RUNTIME_UID" --gid "$RUNTIME_GID" --port "$PORT" --public-url "$PUBLIC_URL"
    if [[ -n "$SETUP_BUNDLE" ]]; then
        echo '2. Importing private setup bundle...'
        python3 -I "$CONFIG" import-setup --env-file "$ENV_FILE" --data-dir "$DATA_DIR" --public-url "$PUBLIC_URL" --bundle-file "$SETUP_BUNDLE"
    fi
else
    # Existing flags are not used to rewrite the authoritative configuration.
    PORT=$(python3 -I "$CONFIG" field --env-file "$ENV_FILE" --field DT_PORT)
    DATA_DIR=$(python3 -I "$CONFIG" field --env-file "$ENV_FILE" --field DT_HOST_DATA_DIR)
    RUNTIME_UID=$(python3 -I "$CONFIG" field --env-file "$ENV_FILE" --field DT_UID)
    RUNTIME_GID=$(python3 -I "$CONFIG" field --env-file "$ENV_FILE" --field DT_GID)
    python3 -I "$CONFIG" init --env-file "$ENV_FILE" --data-dir "$DATA_DIR" --uid "$RUNTIME_UID" --gid "$RUNTIME_GID" --port "$PORT"
    echo 'Existing installation found. Preserving its version, data and all credentials.'
fi
COMPOSE=(docker compose --project-directory "$INSTALL_DIR" --env-file "$ENV_FILE" -f "$INSTALL_DIR/compose.yaml" -p drivetheater)
echo '3. Building and starting DriveTheater...'
"${COMPOSE[@]}" up -d --build
echo '4. Waiting for DriveTheater readiness...'
ready=0
for _ in {1..60}; do
    if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 4 "http://127.0.0.1:$PORT/api/ready" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("status")=="ok" else 1)' >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
done
(( ready )) || fail "Readiness failed. Preserve $ENV_FILE and $DATA_DIR; inspect: sudo bash $INSTALL_DIR/scripts/manage.sh logs"
ADDRESS=$(hostname -I | awk '{print $1}')
printf '\nDriveTheater is ready: http://%s:%s\nData: %s\n' "${ADDRESS:-SERVER_IP}" "$PORT" "$DATA_DIR"
if (( FRESH )); then python3 -I "$CONFIG" credentials --env-file "$ENV_FILE"; fi
printf 'Manage: sudo bash %s/scripts/manage.sh {status|logs|start|stop|backup|login|update}\n' "$INSTALL_DIR"
echo 'HTTP is unencrypted. Use a trusted network/VPN or configure a TLS reverse proxy before using it over an untrusted network.'
}
# Parse the complete function before checkout can replace this script.
main "$@"
