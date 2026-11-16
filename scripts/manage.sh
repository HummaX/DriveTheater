#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE=/etc/drivetheater/install.env
if (( EUID != 0 )); then exec sudo bash "$0" "$@"; fi
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || { echo 'DriveTheater is not initialized; preserve any data and run install.sh first.' >&2; exit 1; }
COMPOSE=(docker compose --project-directory "$ROOT" --env-file "$ENV_FILE" -f "$ROOT/compose.yaml" -p drivetheater)
case "${1:-status}" in
    status) "${COMPOSE[@]}" ps;;
    logs) "${COMPOSE[@]}" logs --tail=150 "${@:2}";;
    start) "${COMPOSE[@]}" up -d;;
    stop) "${COMPOSE[@]}" stop --timeout 30;;
    login) python3 -I "$ROOT/scripts/bootstrap_config.py" credentials --env-file "$ENV_FILE";;
    backup) python3 -I "$ROOT/scripts/bootstrap_config.py" backup --env-file "$ENV_FILE" --revision "$(git -C "$ROOT" rev-parse HEAD)";;
    export-setup) [[ ${2:-} ]] || { echo 'Usage: manage.sh export-setup /absolute/private-setup.json' >&2; exit 2; }; python3 -I "$ROOT/scripts/bootstrap_config.py" export-setup --env-file "$ENV_FILE" --bundle-file "$2";;
    doctor) [[ -f "$ROOT/scripts/doctor.py" ]] || { echo 'Doctor helper is unavailable in this version.' >&2; exit 1; }; exec python3 -I "$ROOT/scripts/doctor.py";;
    recover-network)
        [[ ${2:-} == --confirm-network-reset ]] || { echo 'Usage: manage.sh recover-network --confirm-network-reset (preserves password and MFA)' >&2; exit 2; }
        DATA_DIR=$(python3 -I "$ROOT/scripts/bootstrap_config.py" field --env-file "$ENV_FILE" --field DT_HOST_DATA_DIR)
        exec python3 -I "$ROOT/scripts/security_recovery.py" "$DATA_DIR/db/drivetheater.sqlite3" --confirm-network-reset;;
    encrypt-backup) exec python3 -I "$ROOT/scripts/encrypted_backup.py" encrypt "${@:2}";;
    verify-backup) exec python3 -I "$ROOT/scripts/encrypted_backup.py" verify "${@:2}";;
    https) exec python3 -I "$ROOT/scripts/https_setup.py" "${@:2}";;
    update) exec bash "$ROOT/scripts/update.sh" "${2:-main}";;
    *) echo 'Usage: manage.sh {status|logs|start|stop|backup|encrypt-backup BACKUP_DIR ARCHIVE|verify-backup ARCHIVE|https preflight|render|write [options]|recover-network --confirm-network-reset|export-setup|doctor|login|update [COMMIT_OR_TAG]}' >&2; exit 2;;
esac
