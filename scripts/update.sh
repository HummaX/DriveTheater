#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
main() {
    local root envfile version previous backup target port previous_image
    root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
    envfile=/etc/drivetheater/install.env
    version=${1:-main}
    if (( EUID != 0 )); then exec sudo bash "${BASH_SOURCE[0]}" "$@"; fi
    [[ "$version" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$version" != *..* ]] || { echo 'Invalid version' >&2; return 1; }
    exec 9>/var/lock/drivetheater-install.lock
    flock -n 9 || { echo 'Another install/update is running' >&2; return 1; }
    [[ -f "$envfile" && ! -L "$envfile" ]] || { echo 'Original installation configuration required' >&2; return 1; }
    [[ "$(git -C "$root" config --get remote.origin.url)" == 'https://github.com/HummaX/DriveTheater.git' ]] || { echo 'Unexpected source repository' >&2; return 1; }
    [[ -z "$(git -C "$root" status --porcelain --untracked-files=all)" ]] || { echo 'Local source changes detected; no update performed' >&2; return 1; }
    previous=$(git -C "$root" rev-parse HEAD)
    previous_image=$(docker image inspect drivetheater:local --format '{{.Id}}') || { echo 'Previous application image is missing; preserve the installation and investigate' >&2; return 1; }
    [[ "$previous_image" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo 'Cannot identify previous image' >&2; return 1; }
    git -C "$root" -c core.hooksPath=/dev/null fetch --no-tags origin "$version"
    target=$(git -C "$root" rev-parse FETCH_HEAD)
    if [[ "$previous" == "$target" ]]; then echo 'Already on requested version'; return 0; fi
    local -a compose=(docker compose --project-directory "$root" --env-file "$envfile" -f "$root/compose.yaml" -p drivetheater)
    echo 'Stopping only DriveTheater for a consistent configuration/database backup...'
    "${compose[@]}" stop --timeout 30
    if ! backup=$(python3 -I "$root/scripts/bootstrap_config.py" backup --env-file "$envfile" --revision "$previous"); then
        "${compose[@]}" start
        echo 'Backup failed; restarted the previous application without updating source.' >&2
        return 1
    fi
    "${compose[@]}" start
    echo "Backup: $backup ; previous version: $previous"
    [[ -z "$(git -C "$root" status --porcelain --untracked-files=all)" ]] || { echo 'Source changed; previous services remain running, no checkout attempted' >&2; return 1; }
    git -C "$root" -c core.hooksPath=/dev/null checkout --detach --no-overwrite-ignore "$target"
    # Existing containers retain their image ID while the replacement is built.
    if ! "${compose[@]}" build; then
        git -C "$root" -c core.hooksPath=/dev/null checkout --detach --no-overwrite-ignore "$previous"
        docker image tag "$previous_image" drivetheater:local
        echo "Build failed; previous services were kept running. Backup retained: $backup" >&2
        return 1
    fi
    restore_previous() {
        echo 'Attempting to restore the prior application image and source; persistent data will NOT be rolled back.' >&2
        if "${compose[@]}" stop --timeout 30 &&
           git -C "$root" -c core.hooksPath=/dev/null checkout --detach --no-overwrite-ignore "$previous" &&
           docker image tag "$previous_image" drivetheater:local &&
           "${compose[@]}" up -d --no-build; then
            echo "Prior application restarted. Verify readiness; inspect $backup if the new version changed the database." >&2
        else
            echo "Application recovery failed. Preserve $backup and inspect service logs; no data was restored or deleted." >&2
        fi
    }
    "${compose[@]}" stop --timeout 30
    if ! "${compose[@]}" up -d --no-build; then
        restore_previous
        return 1
    fi
    port=$(python3 -I "$root/scripts/bootstrap_config.py" field --env-file "$envfile" --field DT_PORT)
    for _ in {1..60}; do
        if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 4 "http://127.0.0.1:$port/api/ready" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("status")=="ok" else 1)' >/dev/null 2>&1; then
            echo "Updated to $target ; backup: $backup"; return 0
        fi
        sleep 2
    done
    echo "New version did not become ready; backup retained: $backup" >&2
    restore_previous
    return 1
}
main "$@"
