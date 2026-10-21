#!/usr/bin/env bash
main() {
set -Eeuo pipefail
INSTALL_DIR=""; VERSION=""; SOURCE_ARCHIVE=""; CHECKSUM_FILE=""; ROLLBACK=0; HEALTH_TIMEOUT=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    --version) VERSION="$2"; shift 2;;
    --source-archive) SOURCE_ARCHIVE="$2"; shift 2;;
    --checksum-file) CHECKSUM_FILE="$2"; shift 2;;
    --rollback) ROLLBACK=1; shift;;
    --health-timeout) HEALTH_TIMEOUT="$2"; shift 2;;
    -h|--help) echo 'Usage: update.sh --install-dir DIR [--version VERSION] [--rollback]'; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
[[ -n "$INSTALL_DIR" ]] || { echo '--install-dir is required' >&2; exit 2; }
INSTALL_DIR="$(cd "$INSTALL_DIR" && pwd -P)"
STATE_DIR="$INSTALL_DIR/.lay4flow-updates"
MARKER="$INSTALL_DIR/data/update-maintenance"
LOCK_DIR="$STATE_DIR/update.lock.d"
STAGE=""; MAINTENANCE=0; STOPPED=0
mkdir -p "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then echo "Another update is already running: $LOCK_DIR" >&2; exit 1; fi
cleanup() {
  if (( MAINTENANCE )); then rm -f "$MARKER"; fi
  if (( STOPPED )); then compose up -d --no-build app || true; fi
  if [[ -n "$STAGE" ]]; then rm -rf "$STAGE"; fi
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
command -v docker >/dev/null || { echo 'Docker is required' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required' >&2; exit 1; }
[[ -f "$INSTALL_DIR/docker-compose.yml" ]] || { echo 'docker-compose.yml is missing' >&2; exit 1; }
context="$(docker context show)"
project="lay4flow-remote"
port=38000
if [[ -f "$INSTALL_DIR/.env" ]]; then
  configured_project="$(sed -nE 's/^[[:space:]]*COMPOSE_PROJECT_NAME[[:space:]]*=[[:space:]]*([^[:space:]]+).*$/\1/p' "$INSTALL_DIR/.env" | head -1)"
  configured_port="$(sed -nE 's/^[[:space:]]*LAY4FLOW_REMOTE_PORT[[:space:]]*=[[:space:]]*([0-9]+).*$/\1/p' "$INSTALL_DIR/.env" | head -1)"
  [[ -n "$configured_project" ]] && project="$configured_project"
  [[ -n "$configured_port" ]] && port="$configured_port"
fi
docker_cmd=(docker --context "$context")
compose_args=(compose --project-name "$project" --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/docker-compose.yml")
[[ -f "$INSTALL_DIR/.env" ]] && compose_args+=(--env-file "$INSTALL_DIR/.env")
compose() { local args=("${compose_args[@]}"); [[ -f "$STATE_DIR/image-override.json" ]] && args+=(-f "$STATE_DIR/image-override.json"); "${docker_cmd[@]}" "${args[@]}" "$@"; }
compose config --quiet
container="$(compose ps -q app | head -1)"
[[ -n "$container" ]] || { echo 'Start the existing installation before updating' >&2; exit 1; }
helper_image="$("${docker_cmd[@]}" inspect --format '{{.Image}}' "$container")"
current_image="$(compose config --images | head -1 | tr -d '\r')"
owner="$("${docker_cmd[@]}" inspect --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$container")"
[[ "$owner" == "$INSTALL_DIR" ]] || { echo "Compose project belongs to another install directory: $owner" >&2; exit 1; }
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/lay4flow-update.XXXXXX")"
cp -- "$(dirname "${BASH_SOURCE[0]}")/update_runtime.py" "$STAGE/update_runtime.py"
helper() { "${docker_cmd[@]}" run --rm --entrypoint python --mount "type=bind,source=$INSTALL_DIR,target=/deploy" --mount "type=bind,source=$STAGE,target=/update" "$helper_image" /update/update_runtime.py "$@"; }
json_field() { "${docker_cmd[@]}" run --rm -i --entrypoint python "$helper_image" -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1"; }
health() {
  local expected="${1:-}" deadline=$((SECONDS + HEALTH_TIMEOUT)) response running backend
  while (( SECONDS < deadline )); do
    if response="$(curl -fsS --max-time 8 "http://127.0.0.1:${port}/health")"; then
      running="$(printf '%s' "$response" | json_field version)"
      backend="$(printf '%s' "$response" | json_field backend_running)"
      if [[ "$backend" == True && ( -z "$expected" || "$running" == "$expected" ) ]]; then printf '%s' "$response"; return 0; fi
    fi
    sleep 2
  done
  echo "Health/version check failed on port $port (expected $expected)" >&2
  return 1
}
smoke() {
  curl -fsS --max-time 10 "http://127.0.0.1:${port}/" | grep -qi '<html' || { echo 'Management page smoke check failed' >&2; return 1; }
  status="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${port}/v1/models")"
  [[ "$status" == 401 ]] || { echo "Public model endpoint auth smoke failed (HTTP $status)" >&2; return 1; }
}
restore() {
  local saved="$1" data="$2" image image_id expected
  image="$(cat "$INSTALL_DIR/$saved/metadata.json" | json_field image)"
  image_id="$(cat "$INSTALL_DIR/$saved/metadata.json" | json_field image_id)"
  expected="$(cat "$INSTALL_DIR/$saved/metadata.json" | json_field health_version)"
  compose stop --timeout 120 app
  if (( data )); then helper restore --backup "/deploy/$saved" --restore-data; else helper restore --backup "/deploy/$saved"; fi
  if [[ -n "$image_id" && "$image_id" != "$image" ]]; then "${docker_cmd[@]}" image tag "$image_id" "$image"; fi
  export LAY4FLOW_REMOTE_IMAGE="$image"
  compose up -d --no-build --force-recreate app
  health "$expected" >/dev/null; smoke
}
if (( ROLLBACK )); then
  backup="$(find "$STATE_DIR/backups" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r | head -1)"
  [[ -n "$backup" && -f "$STATE_DIR/backups/$backup/metadata.json" ]] || { echo 'No update backup exists' >&2; exit 1; }
  backup_path=".lay4flow-updates/backups/$backup"
else
  if [[ -z "$VERSION" ]]; then VERSION="$(curl -fsSL -H 'User-Agent: lay4flow-remote-updater' https://api.github.com/repos/TheLayya/lay4flow/releases/latest | json_field tag_name)"; fi
  VERSION="${VERSION#v}"
  [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]] || { echo 'Invalid release version' >&2; exit 1; }
  base="https://github.com/TheLayya/lay4flow/releases/download/v${VERSION}"
  if [[ -n "$SOURCE_ARCHIVE" ]]; then cp -- "$SOURCE_ARCHIVE" "$STAGE/runtime.tar.gz"; else curl -fL --retry 2 -o "$STAGE/runtime.tar.gz" "$base/lay4flow-remote-${VERSION}-runtime.tar.gz"; fi
  if [[ -n "$CHECKSUM_FILE" ]]; then cp -- "$CHECKSUM_FILE" "$STAGE/SHA256SUMS"; else curl -fL --retry 2 -o "$STAGE/SHA256SUMS" "$base/SHA256SUMS"; fi
  helper prepare --version "$VERSION"
  stamp="$(date -u +%Y%m%d-%H%M%S)-$$"
  candidate="lay4flow-remote:update-${VERSION}-${stamp}"
  echo 'Building verified candidate while the current service keeps running...'
  "${docker_cmd[@]}" build --build-arg "APP_VERSION=$VERSION" --tag "$candidate" "$STAGE/candidate"
  backup_path=".lay4flow-updates/backups/$stamp"
fi
[[ ! -e "$MARKER" ]] || { echo "An update maintenance marker already exists: $MARKER" >&2; exit 1; }
mkdir -p "$INSTALL_DIR/data"
printf 'host updater\n' > "$MARKER"
MAINTENANCE=1
sleep 1
response="$(health)"
active="$(printf '%s' "$response" | json_field update_active_requests)"
imports="$(printf '%s' "$response" | json_field update_pending_imports)"
if [[ "${active:-0}" != 0 || "${imports:-0}" != 0 ]]; then echo 'Active requests or Profile imports remain; retry after they finish' >&2; exit 1; fi
helper idle
if (( ROLLBACK )); then restore "$backup_path" 0; echo 'Rollback complete; current account data was kept.'; exit 0; fi
echo 'Switching app container; a brief service interruption is expected.'
compose stop --timeout 120 app
STOPPED=1
helper snapshot --backup "/deploy/$backup_path" --image "$current_image" --image-id "$helper_image" --health-version "$(printf '%s' "$response" | json_field version)"
if helper apply && helper set-image --image "$candidate"; then
  export LAY4FLOW_REMOTE_IMAGE="$candidate"
  if compose up -d --no-build --force-recreate app && health "$VERSION" >/dev/null && smoke; then
    STOPPED=0
    printf '%s\n%s\n' "$VERSION" "$backup_path" > "$STATE_DIR/last-success.txt"
    echo "Update complete: lay4flow-remote $VERSION"
    exit 0
  fi
fi
echo 'Candidate failed; restoring previous image, files and data snapshot.' >&2
restore "$backup_path" 1
STOPPED=0
exit 1
}
main "$@"
