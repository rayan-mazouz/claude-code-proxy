#!/usr/bin/env bash
#
# Install, update, back up, inspect, or remove Claude Code Proxy on a Debian or
# Ubuntu server, under a dedicated unprivileged user running rootless Docker.
#
#   sudo ./server-install.sh install
#   sudo ./server-install.sh update
#   sudo ./server-install.sh backup
#   sudo ./server-install.sh status
#   sudo ./server-install.sh logs [service]
#   sudo ./server-install.sh uninstall [--yes]
#
# The script is safe to re-run. `install` on a server that already has an
# installation behaves like `update`: it backs up the database first and keeps
# every existing setting in .env.
#
# Optional overrides. A fresh install applies all of them (with the defaults
# below); on an existing installation only the ones you set change:
#
#   CCP_REPO_URL       Git repository to deploy (default: the rayan-mazouz fork)
#   CCP_REPO_BRANCH    Branch to deploy (default: main)
#   CCP_HTTP_PORT      Host port Traefik listens on (default: 18437)
#   CCP_BIND_ADDRESS   Host IPv4 address to bind, e.g. a VPN interface IP (default: 0.0.0.0)
#   CCP_DOMAIN         Hostname used in dashboard links (default: localhost)
#   CCP_ALLOWED_MODELS Comma-separated model allowlist (default: claude-opus-5-5,claude-sonnet-5-5)
#
# The checkout is never edited: server-specific Compose changes live in
# ${OVERRIDE_FILE}, and all settings live in the checkout's ignored .env.

set -Eeuo pipefail
umask 022
trap 'echo "ERROR: line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

APP_USER="claudeproxy"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/claude-code-proxy"
OVERRIDE_FILE="${APP_HOME}/docker-compose.server.yml"
COMPOSE_PROJECT="claude-code-proxy"

STATE_DIR="/etc/claude-code-proxy"
STATE_FILE="${STATE_DIR}/rootless.conf"
CREDS_FILE="/root/claude-code-proxy-admin.txt"
BACKUP_ROOT="/root/claude-code-proxy-backups"
BACKUPS_TO_KEEP=10
LOCK_FILE="/run/lock/claude-code-proxy-installer.lock"

DEFAULT_REPO_URL="https://github.com/rayan-mazouz/claude-code-proxy.git"
DEFAULT_HTTP_PORT=18437
DEFAULT_BIND_ADDRESS="0.0.0.0"
DEFAULT_DOMAIN="localhost"
DEFAULT_ALLOWED_MODELS="claude-opus-5-5,claude-sonnet-5-5"

REPO_URL="${CCP_REPO_URL:-${DEFAULT_REPO_URL}}"
REPO_BRANCH="${CCP_REPO_BRANCH:-main}"

APPARMOR_PROFILE="/etc/apparmor.d/usr.bin.rootlesskit"
DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-ce-rootless-extras)
MIN_COMPOSE_VERSION="2.24.4" # first release supporting the !override tag

# Recorded in ${STATE_FILE} so uninstall only removes Docker if this script installed it.
DOCKER_INSTALLED_BY_SCRIPT=0
# First line of the AppArmor profile this script writes; uninstall only removes a profile carrying it.
# The text names the script's original file name and must not change, or existing profiles would be orphaned.
APPARMOR_MARKER="# Managed by claude-code-proxy server-install-script.sh"

LAST_BACKUP_DIR=""

#
# Output and prompts
#

log() {
    printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

warn() {
    echo "WARNING: $*" >&2
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

confirm() {
    # confirm <prompt> <default y|n>
    local prompt="$1" default="$2" answer=""

    [[ -t 0 ]] || die "No terminal to answer \"${prompt}\"."

    if [[ "${default}" == y ]]; then
        read -r -p "${prompt} [Y/n] " answer || true
        [[ ! "${answer}" =~ ^[Nn] ]]
    else
        read -r -p "${prompt} [y/N] " answer || true
        [[ "${answer}" =~ ^[Yy] ]]
    fi
}

#
# Input validation. Every value ends up in .env and in Compose labels, so only
# well-formed values are accepted.
#

valid_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

valid_ipv4() {
    local IFS=. octet
    local -a octets
    [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    read -r -a octets <<<"$1"
    for octet in "${octets[@]}"; do
        ((10#${octet} <= 255)) || return 1
    done
}

valid_hostname() {
    [[ "${#1}" -le 253 && "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]]
}

valid_models() {
    [[ "$1" =~ ^[A-Za-z0-9._:-]+(,[A-Za-z0-9._:-]+)*$ ]]
}

validate_overrides() {
    if [[ -n "${CCP_HTTP_PORT:-}" ]] && ! valid_port "${CCP_HTTP_PORT}"; then
        die "CCP_HTTP_PORT must be a port number between 1 and 65535."
    fi
    if [[ -n "${CCP_BIND_ADDRESS:-}" ]] && ! valid_ipv4 "${CCP_BIND_ADDRESS}"; then
        die "CCP_BIND_ADDRESS must be an IPv4 address such as 0.0.0.0 or 10.8.0.1."
    fi
    if [[ -n "${CCP_DOMAIN:-}" ]] && ! valid_hostname "${CCP_DOMAIN}"; then
        die "CCP_DOMAIN must be a plain hostname such as proxy.example.com (no scheme, port or path)."
    fi
    if [[ -n "${CCP_ALLOWED_MODELS:-}" ]] && ! valid_models "${CCP_ALLOWED_MODELS}"; then
        die "CCP_ALLOWED_MODELS must be a comma-separated list of model IDs without spaces."
    fi
    [[ "${REPO_BRANCH}" =~ ^[A-Za-z0-9._/-]+$ ]] || die "CCP_REPO_BRANCH contains unsupported characters."
    [[ "${REPO_URL}" =~ ^(https://|git@|ssh://)[^[:space:]]+$ ]] || die "CCP_REPO_URL must be an https://, ssh:// or git@ URL."
}

#
# Helpers
#

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "Run this script with sudo."
}

take_lock() {
    exec 9>"${LOCK_FILE}"
    flock -n 9 || die "Another run of this script is in progress."
}

user_exists() {
    id "${APP_USER}" >/dev/null 2>&1
}

get_uid() {
    id -u "${APP_USER}"
}

installed() {
    [[ -d "${APP_DIR}/.git" && -f "${APP_DIR}/.env" ]]
}

# Run a command as the application user with a clean, explicit environment, so
# root's HOME, DOCKER_HOST, or SUDO_* variables never leak into it.
as_app() {
    local uid
    uid="$(get_uid)"

    runuser -u "${APP_USER}" -- env -i \
        HOME="${APP_HOME}" \
        USER="${APP_USER}" \
        LOGNAME="${APP_USER}" \
        SHELL=/bin/bash \
        LANG=C.UTF-8 \
        TERM="${TERM:-dumb}" \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        XDG_RUNTIME_DIR="/run/user/${uid}" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
        DOCKER_HOST="unix:///run/user/${uid}/docker.sock" \
        "$@"
}

app_git() {
    as_app git -C "${APP_DIR}" "$@"
}

docker_socket_ready() {
    user_exists && [[ -S "/run/user/$(get_uid)/docker.sock" ]]
}

# Compose resolves relative build contexts against the first file's directory,
# so always run it from inside the checkout. The inner script is single-quoted on
# purpose: its arguments are expanded by the application user's shell.
compose() {
    # shellcheck disable=SC2016
    as_app bash -c '
        app_dir="$1"; override="$2"; project="$3"; shift 3
        cd -- "${app_dir}" || exit 1
        exec docker compose \
            --project-name "${project}" \
            -f docker-compose.yaml \
            -f docker-compose.postgres.yml \
            -f docker-compose.traefik.yml \
            -f "${override}" \
            "$@"
    ' bash "${APP_DIR}" "${OVERRIDE_FILE}" "${COMPOSE_PROJECT}" "$@"
}

stack_running() {
    docker_socket_ready && [[ -f "${OVERRIDE_FILE}" && -d "${APP_DIR}" ]] &&
        compose ps --status running --services 2>/dev/null | grep -qx postgres
}

env_get() {
    local key="$1" file="${APP_DIR}/.env"

    [[ -f "${file}" ]] || return 0
    sed -n "s/^${key}=//p" "${file}" | tail -n 1
}

# Replace KEY=... in place (or append it) without interpreting the value.
set_env_value() {
    local key="$1" value="$2" file="${APP_DIR}/.env" tmp

    tmp="$(mktemp)"
    KEY="${key}" VALUE="${value}" awk '
        BEGIN { done = 0 }
        index($0, ENVIRON["KEY"] "=") == 1 {
            if (!done) { print ENVIRON["KEY"] "=" ENVIRON["VALUE"]; done = 1 }
            next
        }
        { print }
        END { if (!done) print ENVIRON["KEY"] "=" ENVIRON["VALUE"] }
    ' "${file}" >"${tmp}"
    # Rewrite in place so the file keeps its owner and 0600 mode.
    cat "${tmp}" >"${file}"
    rm -f "${tmp}"
}

# Set KEY only when .env does not define it yet.
default_env_value() {
    [[ -n "$(env_get "$1")" ]] || set_env_value "$1" "$2"
}

load_state() {
    [[ -f "${STATE_FILE}" ]] || return 0

    local value
    value="$(sed -n 's/^DOCKER_INSTALLED_BY_SCRIPT=//p' "${STATE_FILE}" | tail -n 1)"
    [[ "${value}" == 1 ]] && DOCKER_INSTALLED_BY_SCRIPT=1

    # Keep deploying from the recorded repository unless the caller chose another.
    if [[ -z "${CCP_REPO_URL:-}" ]]; then
        value="$(sed -n 's/^REPO_URL=//p' "${STATE_FILE}" | tail -n 1)"
        [[ -n "${value}" ]] && REPO_URL="${value}"
    fi
    if [[ -z "${CCP_REPO_BRANCH:-}" ]]; then
        value="$(sed -n 's/^REPO_BRANCH=//p' "${STATE_FILE}" | tail -n 1)"
        [[ -n "${value}" ]] && REPO_BRANCH="${value}"
    fi
    return 0
}

save_state() {
    install -d -m 0700 "${STATE_DIR}"

    cat >"${STATE_FILE}" <<EOF
APP_USER=${APP_USER}
APP_HOME=${APP_HOME}
APP_DIR=${APP_DIR}
REPO_URL=${REPO_URL}
REPO_BRANCH=${REPO_BRANCH}
DOCKER_INSTALLED_BY_SCRIPT=${DOCKER_INSTALLED_BY_SCRIPT}
EOF
    chmod 0600 "${STATE_FILE}"
}

#
# Host prerequisites
#

apt_install() {
    export DEBIAN_FRONTEND=noninteractive

    log "Installing host packages."
    apt-get update
    apt-get install -y \
        ca-certificates \
        curl \
        git \
        openssl \
        uidmap \
        dbus-user-session \
        slirp4netns \
        fuse-overlayfs \
        iptables \
        iproute2 \
        passwd \
        procps \
        coreutils \
        util-linux \
        gzip
}

ensure_docker() {
    if command -v docker >/dev/null 2>&1 &&
        command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1; then
        log "Docker and rootless tooling already installed."
        return
    fi

    if dpkg-query -W -f='${Status}' docker.io 2>/dev/null | grep -q "install ok installed"; then
        die "Ubuntu's docker.io package is installed; it conflicts with Docker CE's rootless extras. Remove it first."
    fi

    log "Installing Docker CE and rootless tooling."

    # shellcheck disable=SC1091
    . /etc/os-release
    local distro="${ID}" codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    if [[ "${distro}" != ubuntu && "${distro}" != debian ]]; then
        if [[ "${ID_LIKE:-}" == *ubuntu* ]]; then
            distro=ubuntu
        else
            die "Unsupported distribution: ${ID}"
        fi
    fi
    [[ -n "${codename}" ]] || die "Could not determine the distribution codename."

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${distro}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' \
        "$(dpkg --print-architecture)" "${distro}" "${codename}" >/etc/apt/sources.list.d/docker.list
    chmod 0644 /etc/apt/sources.list.d/docker.list

    apt-get update
    apt-get install -y "${DOCKER_PACKAGES[@]}"
    DOCKER_INSTALLED_BY_SCRIPT=1

    command -v docker >/dev/null 2>&1 || die "Docker installation failed."
    command -v dockerd-rootless-setuptool.sh >/dev/null 2>&1 || die "dockerd-rootless-setuptool.sh is unavailable."
}

rootlesskit_works() {
    as_app rootlesskit true >/dev/null 2>&1
}

# Ubuntu 24.04+ blocks unprivileged user namespaces unless the binary has an
# AppArmor profile allowing them. Recent releases ship one for rootlesskit, and
# a second profile for the same binary makes AppArmor refuse to run it, so only
# add ours when rootlesskit is actually blocked and nothing else covers it.
ensure_rootlesskit_userns() {
    local knob=/proc/sys/kernel/apparmor_restrict_unprivileged_userns others

    rootlesskit_works && return 0

    [[ -r "${knob}" && "$(<"${knob}")" == 1 ]] ||
        die "rootlesskit cannot start; see: sudo runuser -u ${APP_USER} -- rootlesskit true"
    command -v apparmor_parser >/dev/null 2>&1 || die "AppArmor restricts user namespaces but apparmor_parser is missing."

    others="$(grep -rlE '^[^#]*/usr/bin/rootlesskit' /etc/apparmor.d --exclude="${APPARMOR_PROFILE##*/}" 2>/dev/null || true)"
    if [[ -n "${others}" ]]; then
        die "rootlesskit is blocked although AppArmor profiles already cover it: ${others//$'\n'/ }. Check: sudo dmesg | grep -i apparmor | tail"
    fi
    if [[ -e "${APPARMOR_PROFILE}" ]] && ! grep -qxF "${APPARMOR_MARKER}" "${APPARMOR_PROFILE}"; then
        die "rootlesskit is blocked although ${APPARMOR_PROFILE} exists (not written by this script). Check it is loaded: sudo aa-status"
    fi

    log "Allowing rootlesskit to create user namespaces (AppArmor)."
    cat >"${APPARMOR_PROFILE}" <<EOF
${APPARMOR_MARKER}
abi <abi/4.0>,
include <tunables/global>

/usr/bin/rootlesskit flags=(unconfined) {
  userns,

  include if exists <local/usr.bin.rootlesskit>
}
EOF
    chmod 0644 "${APPARMOR_PROFILE}"
    apparmor_parser -r "${APPARMOR_PROFILE}"

    rootlesskit_works || die "rootlesskit is still blocked after loading ${APPARMOR_PROFILE}. Check: sudo dmesg | grep -i apparmor | tail"
}

#
# Application user and rootless Docker
#

# First subordinate ID above every range already allocated, so this user's
# container IDs never overlap another user's.
next_free_subid() {
    local highest=100000 file start count

    for file in /etc/subuid /etc/subgid; do
        [[ -f "${file}" ]] || continue
        while IFS=: read -r _ start count; do
            [[ "${start}" =~ ^[0-9]+$ && "${count}" =~ ^[0-9]+$ ]] || continue
            ((start + count > highest)) && highest=$((start + count))
        done <"${file}"
    done
    echo "${highest}"
}

warn_on_subid_overlap() {
    local file own_start own_count user start count

    for file in /etc/subuid /etc/subgid; do
        IFS=: read -r _ own_start own_count < <(grep "^${APP_USER}:" "${file}" 2>/dev/null) || continue
        while IFS=: read -r user start count; do
            [[ "${user}" == "${APP_USER}" || ! "${start}" =~ ^[0-9]+$ || ! "${count}" =~ ^[0-9]+$ ]] && continue
            if ((start < own_start + own_count && own_start < start + count)); then
                warn "${APP_USER}'s range in ${file} overlaps ${user}'s; their containers can share host IDs."
            fi
        done <"${file}"
    done
}

ensure_user() {
    if ! user_exists; then
        log "Creating system user ${APP_USER}."
        useradd --system --create-home --home-dir "${APP_HOME}" --shell /bin/bash "${APP_USER}"
    fi

    passwd -l "${APP_USER}" >/dev/null 2>&1 || true
    gpasswd -d "${APP_USER}" sudo >/dev/null 2>&1 || true
    gpasswd -d "${APP_USER}" docker >/dev/null 2>&1 || true

    install -d -m 0750 -o "${APP_USER}" -g "${APP_USER}" "${APP_HOME}"

    if ! grep -q "^${APP_USER}:" /etc/subuid 2>/dev/null || ! grep -q "^${APP_USER}:" /etc/subgid 2>/dev/null; then
        local start
        start="$(next_free_subid)"
        grep -q "^${APP_USER}:" /etc/subuid 2>/dev/null || usermod --add-subuids "${start}-$((start + 65535))" "${APP_USER}"
        grep -q "^${APP_USER}:" /etc/subgid 2>/dev/null || usermod --add-subgids "${start}-$((start + 65535))" "${APP_USER}"
    fi
    warn_on_subid_overlap
}

enable_linger() {
    local uid
    uid="$(get_uid)"

    loginctl enable-linger "${APP_USER}"
    systemctl start "user@${uid}.service"

    for _ in {1..30}; do
        [[ -S "/run/user/${uid}/bus" ]] && return
        sleep 1
    done
    die "The systemd user manager for ${APP_USER} did not start."
}

ensure_rootless_docker() {
    local socket
    socket="/run/user/$(get_uid)/docker.sock"

    if [[ ! -f "${APP_HOME}/.config/systemd/user/docker.service" ]]; then
        ensure_rootlesskit_userns
        log "Setting up rootless Docker for ${APP_USER}."
        as_app dockerd-rootless-setuptool.sh install
    fi

    as_app systemctl --user daemon-reload
    as_app systemctl --user enable --now docker.service

    for _ in {1..60}; do
        [[ -S "${socket}" ]] && break
        sleep 1
    done
    [[ -S "${socket}" ]] || die "Rootless Docker socket did not appear: ${socket}"

    as_app docker info >/dev/null
}

prepare_host() {
    apt_install
    ensure_docker
    ensure_user
    enable_linger
    ensure_rootless_docker
    save_state
}

#
# Application checkout and configuration
#

ensure_repo() {
    if [[ -d "${APP_DIR}/.git" ]]; then
        app_git remote set-url origin "${REPO_URL}"
        return
    fi

    if [[ -e "${APP_DIR}" ]]; then
        local moved
        moved="${APP_DIR}.preinstall.$(date '+%Y%m%d-%H%M%S')"
        log "Moving existing non-Git ${APP_DIR} to ${moved}."
        mv "${APP_DIR}" "${moved}"
    fi

    log "Cloning ${REPO_URL} (${REPO_BRANCH})."
    as_app git clone --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${APP_DIR}"
}

update_repo() {
    log "Fetching ${REPO_BRANCH} from ${REPO_URL}."
    app_git remote set-url origin "${REPO_URL}"
    app_git fetch --prune origin "+refs/heads/${REPO_BRANCH}:refs/remotes/origin/${REPO_BRANCH}"

    # The checkout is managed by this script; keep any local edits in the backup, then discard them.
    if [[ -n "$(app_git status --porcelain --untracked-files=no)" ]]; then
        local patch
        patch="${LAST_BACKUP_DIR:-${BACKUP_ROOT}}/local-changes-$(date '+%Y%m%d-%H%M%S').patch"
        install -d -m 0700 "$(dirname "${patch}")"
        app_git diff HEAD >"${patch}"
        warn "Discarding local changes to tracked files (saved to ${patch})."
    fi

    app_git reset -q --hard
    app_git checkout -q -B "${REPO_BRANCH}" "origin/${REPO_BRANCH}"
    log "Now at $(app_git log -1 --format='%h %s')."
}

generate_secret() {
    openssl rand -hex 32
}

generate_fernet_key() {
    openssl rand -base64 32 | tr '+/' '-_'
}

is_unset_secret() {
    [[ -z "$1" || "$1" == change-me* || "$1" == changeme ]]
}

postgres_volume_exists() {
    as_app docker volume inspect "${COMPOSE_PROJECT}_postgres-data" >/dev/null 2>&1
}

# Create .env when missing, generate any missing secret, and fill in settings
# that are not set yet. Existing values are never replaced here; explicit
# CCP_* overrides are applied separately by apply_env_overrides.
configure_env() {
    local env_file="${APP_DIR}/.env" fresh=0

    if [[ ! -f "${env_file}" ]]; then
        fresh=1
        if [[ -f "${APP_DIR}/.env.example" ]]; then
            cp "${APP_DIR}/.env.example" "${env_file}"
        else
            : >"${env_file}"
        fi
    fi
    chown "${APP_USER}:${APP_USER}" "${env_file}"
    chmod 0600 "${env_file}"

    local key value
    for key in JWT_SECRET ADMIN_PASSWORD; do
        value="$(env_get "${key}")"
        is_unset_secret "${value}" && set_env_value "${key}" "$(generate_secret)"
    done

    # An empty key means the backend never started, so nothing is encrypted with it yet.
    is_unset_secret "$(env_get FERNET_KEY)" && set_env_value FERNET_KEY "$(generate_fernet_key)"

    # Postgres only reads its password when the volume is first initialised.
    value="$(env_get POSTGRES_PASSWORD)"
    if is_unset_secret "${value}"; then
        if postgres_volume_exists; then
            warn "POSTGRES_PASSWORD is a placeholder but the database already exists; leaving it unchanged."
        else
            set_env_value POSTGRES_PASSWORD "$(generate_secret)"
        fi
    fi

    if ((fresh)); then
        # .env.example ships development defaults; replace them for this server.
        set_env_value ENV production
        set_env_value LOG_LEVEL INFO
        set_env_value HTTP_PORT "${DEFAULT_HTTP_PORT}"
        set_env_value HTTP_BIND_ADDRESS "${DEFAULT_BIND_ADDRESS}"
        set_env_value DOMAIN "${DEFAULT_DOMAIN}"
        set_env_value ALLOWED_MODELS "${DEFAULT_ALLOWED_MODELS}"
        set_env_value ARCHIVE_ENABLED false
    fi

    default_env_value ENV production
    default_env_value LOG_LEVEL INFO
    default_env_value HTTP_PORT "${DEFAULT_HTTP_PORT}"
    default_env_value HTTP_BIND_ADDRESS "${DEFAULT_BIND_ADDRESS}"
    default_env_value DOMAIN "${DEFAULT_DOMAIN}"
    default_env_value ALLOWED_MODELS "${DEFAULT_ALLOWED_MODELS}"
    default_env_value ARCHIVE_ENABLED false
    default_env_value POSTGRES_HOST postgres
    default_env_value POSTGRES_PORT 5432
    default_env_value POSTGRES_USER claude_proxy
    default_env_value POSTGRES_DB claude_code_proxy
    default_env_value ADMIN_USERNAME admin

    apply_env_overrides
    write_credentials
}

# Apply only the settings explicitly passed through CCP_*; everything else in
# .env (including manual edits) is kept.
apply_env_overrides() {
    [[ -n "${CCP_HTTP_PORT:-}" ]] && set_env_value HTTP_PORT "${CCP_HTTP_PORT}"
    [[ -n "${CCP_BIND_ADDRESS:-}" ]] && set_env_value HTTP_BIND_ADDRESS "${CCP_BIND_ADDRESS}"
    [[ -n "${CCP_DOMAIN:-}" ]] && set_env_value DOMAIN "${CCP_DOMAIN}"
    [[ -n "${CCP_ALLOWED_MODELS:-}" ]] && set_env_value ALLOWED_MODELS "${CCP_ALLOWED_MODELS}"
    return 0
}

write_credentials() {
    (
        umask 077
        cat >"${CREDS_FILE}" <<EOF
Claude Code Proxy administrator credentials
===========================================

Username: $(env_get ADMIN_USERNAME)
Password: $(env_get ADMIN_PASSWORD)

Dashboard: http://$(advertised_host):$(env_get HTTP_PORT)/
Application directory: ${APP_DIR}
Allowed models: $(env_get ALLOWED_MODELS)
EOF
    )
}

# Everything this server changes about the upstream Compose files. Lives outside
# the checkout so updates never hit local modifications.
write_override() {
    local uid
    uid="$(get_uid)"

    log "Writing ${OVERRIDE_FILE}."
    cat >"${OVERRIDE_FILE}" <<EOF
# Generated by scripts/server-install.sh on every install/update; edits are overwritten.
services:
  traefik:
    # HTTP only, on the configured address. TLS (443) is left to the host's own proxy.
    ports: !override
      - "\${HTTP_BIND_ADDRESS:-0.0.0.0}:\${HTTP_PORT:-${DEFAULT_HTTP_PORT}}:80"
    # Rootless Docker's socket instead of the system one.
    volumes: !override
      - /run/user/${uid}/docker.sock:/var/run/docker.sock:ro
      - traefik-letsencrypt:/letsencrypt
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"

  postgres:
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"

  # Route by path only, so the dashboard and API answer on any address (VPN/LAN IP).
  backend:
    labels:
      - traefik.http.routers.claude-code-proxy-api-\${BG_SLOT:-stable}.rule=PathPrefix(\`/api\`)
  frontend:
    labels:
      - traefik.http.routers.claude-code-proxy-\${BG_SLOT:-stable}.rule=PathPrefix(\`/\`)

  # Archives are disabled, and its host bind mount is not writable rootless.
  archive-retention:
    profiles:
      - archive-disabled
EOF
    chown "${APP_USER}:${APP_USER}" "${OVERRIDE_FILE}"
    chmod 0644 "${OVERRIDE_FILE}"
}

#
# Deployment
#

version_at_least() {
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" == "$2" ]]
}

validate_compose() {
    local version
    version="$(as_app docker compose version --short)"
    version_at_least "${version#v}" "${MIN_COMPOSE_VERSION}" ||
        die "Docker Compose ${version} is too old; ${MIN_COMPOSE_VERSION}+ is required."

    log "Validating Compose configuration."
    compose config --quiet
}

ensure_port_free() {
    local port
    port="$(env_get HTTP_PORT)"

    ss -Hltn "sport = :${port}" | grep -q . || return 0
    # Already ours: Traefik from this stack publishes it.
    compose port traefik 80 2>/dev/null | grep -q ":${port}$" && return 0
    die "Port ${port} is already in use by something else; pick another with CCP_HTTP_PORT."
}

# Images are built before anything is stopped, so the running stack keeps
# serving until the new containers are ready to replace it.
start_stack() {
    log "Building images (pulling fresh base images)."
    compose build --pull
    compose pull --ignore-buildable --quiet

    log "Starting Claude Code Proxy."
    compose up -d --remove-orphans
}

# The address users should browse to (a placeholder when bound to every interface).
advertised_host() {
    local bind
    bind="$(env_get HTTP_BIND_ADDRESS)"
    if [[ -z "${bind}" || "${bind}" == 0.0.0.0 ]]; then
        echo "<server-ip>"
    else
        echo "${bind}"
    fi
}

# The address this script can reach the stack on from the host itself.
display_host() {
    local bind
    bind="$(env_get HTTP_BIND_ADDRESS)"
    if [[ -z "${bind}" || "${bind}" == 0.0.0.0 ]]; then
        echo 127.0.0.1
    else
        echo "${bind}"
    fi
}

wait_for_health() {
    local base
    base="http://$(display_host):$(env_get HTTP_PORT)"

    log "Waiting for ${base}/api/health and the dashboard."
    for _ in {1..90}; do
        if curl -fsS -o /dev/null --max-time 3 "${base}/api/health" &&
            curl -fsS -o /dev/null --max-time 3 "${base}/"; then
            log "Healthy: ${base}"
            return
        fi
        sleep 2
    done

    compose ps || true
    die "The stack did not become healthy; inspect it with: sudo $0 logs"
}

prune_images() {
    as_app docker image prune -f >/dev/null || true
}

deploy() {
    ensure_port_free
    write_override
    save_state
    validate_compose
    start_stack
    wait_for_health
    prune_images
}

print_summary() {
    local port bind addresses address
    port="$(env_get HTTP_PORT)"
    bind="$(env_get HTTP_BIND_ADDRESS)"

    if [[ "${bind}" =~ ^(0\.0\.0\.0)?$ ]]; then
        addresses="$(hostname -I 2>/dev/null || true)"
    else
        addresses="${bind}"
    fi

    echo
    echo "============================================================"
    echo " Claude Code Proxy is running"
    echo "============================================================"
    echo
    echo "Dashboard / API base:"
    for address in ${addresses:-<server-ip>}; do
        [[ "${address}" == *:* ]] && continue # skip IPv6
        echo "  http://${address}:${port}/      (API: http://${address}:${port}/api)"
    done
    echo
    echo "Admin credentials:  ${CREDS_FILE}"
    echo "Deployed from:      ${REPO_URL} (${REPO_BRANCH} @ $(app_git rev-parse --short HEAD))"
    echo "Allowed models:     $(env_get ALLOWED_MODELS)"
    echo "Archives:           $([[ "$(env_get ARCHIVE_ENABLED)" == true ]] && echo enabled || echo disabled)"
    echo "Backups:            ${BACKUP_ROOT}"
    echo
    echo "Ports 80/443, the host's own proxy (e.g. Caddy), firewall, and VPN were not touched."
    if [[ "${bind}" =~ ^(0\.0\.0\.0)?$ ]]; then
        echo
        echo "NOTE: the stack serves plain HTTP on every interface. Put it behind a TLS-terminating"
        echo "      proxy, or bind it to a private address with CCP_BIND_ADDRESS, before sharing keys."
    fi
    echo "============================================================"
}

#
# Backups
#

# Dump the database and copy .env into a new timestamped directory. Returns
# non-zero only when a running database could not be dumped.
backup_now() {
    local label="$1" dir

    dir="${BACKUP_ROOT}/$(date '+%Y%m%d-%H%M%S')-${label}"
    install -d -m 0700 "${BACKUP_ROOT}" "${dir}"
    LAST_BACKUP_DIR="${dir}"
    log "Backing up to ${dir}."

    [[ -f "${APP_DIR}/.env" ]] && cp -a "${APP_DIR}/.env" "${dir}/.env"
    app_git rev-parse HEAD >"${dir}/git-revision" 2>/dev/null || true

    if stack_running; then
        # Dump to a temporary name so an interrupted dump never looks complete.
        if compose exec -T postgres pg_dump -U "$(env_get POSTGRES_USER)" -d "$(env_get POSTGRES_DB)" --no-owner |
            gzip >"${dir}/postgres.sql.gz.partial" &&
            gzip -t "${dir}/postgres.sql.gz.partial"; then
            mv "${dir}/postgres.sql.gz.partial" "${dir}/postgres.sql.gz"
            log "Database dumped to ${dir}/postgres.sql.gz."
        else
            rm -f "${dir}/postgres.sql.gz.partial"
            warn "Database dump failed."
            return 1
        fi
    else
        warn "Postgres is not running; no database dump taken."
    fi

    prune_backups
}

# Keep only the newest backups.
prune_backups() {
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' |
        sort -r | tail -n +"$((BACKUPS_TO_KEEP + 1))" |
        while read -r old; do rm -rf -- "${BACKUP_ROOT:?}/${old}"; done
}

#
# Commands
#

cmd_install() {
    if installed; then
        log "An installation already exists in ${APP_DIR}; updating it and keeping its settings."
        cmd_update
        print_summary
        return
    fi

    log "Starting installation."
    prepare_host
    ensure_repo
    configure_env
    deploy
    print_summary
}

cmd_update() {
    [[ -d "${APP_DIR}/.git" ]] || die "Installation not found at ${APP_DIR}. Run install first."

    prepare_host

    backup_now update || confirm "Continue the update without a database backup?" n || die "Update aborted."
    update_repo
    configure_env
    deploy

    log "Update complete."
    compose ps
}

cmd_backup() {
    installed || die "Installation not found at ${APP_DIR}."
    stack_running || die "Postgres is not running; start the stack before taking a backup."
    backup_now manual || die "Backup failed."
    log "Backup complete: ${LAST_BACKUP_DIR}"
    echo "Restore a dump into a running stack with:"
    echo "  gunzip -c ${LAST_BACKUP_DIR}/postgres.sql.gz | sudo runuser -u ${APP_USER} -- \\"
    echo "    env XDG_RUNTIME_DIR=/run/user/$(get_uid) DOCKER_HOST=unix:///run/user/$(get_uid)/docker.sock \\"
    echo "    docker exec -i ${COMPOSE_PROJECT}-postgres psql -U $(env_get POSTGRES_USER) -d $(env_get POSTGRES_DB)"
}

cmd_status() {
    if ! user_exists; then
        echo "Not installed (user ${APP_USER} does not exist)."
        return
    fi

    echo "=== Deployment ==="
    if [[ -d "${APP_DIR}/.git" ]]; then
        echo "Repository: $(app_git remote get-url origin)"
        echo "Revision:   $(app_git log -1 --format='%h %s (%cr)')"
    else
        echo "Repository: missing (${APP_DIR})"
    fi
    echo "Linger:     $(loginctl show-user "${APP_USER}" -p Linger --value 2>/dev/null || echo unknown)"
    echo "Docker:     $(as_app systemctl --user is-active docker.service 2>/dev/null || true)"
    warn_on_subid_overlap

    if docker_socket_ready && [[ -f "${OVERRIDE_FILE}" ]]; then
        echo
        echo "=== Containers ==="
        compose ps || true

        echo
        echo "=== Health ==="
        local url
        url="http://$(display_host):$(env_get HTTP_PORT)/api/health"
        if curl -fsS -o /dev/null --max-time 3 "${url}"; then echo "OK      ${url}"; else echo "FAILING ${url}"; fi
    fi

    echo
    echo "=== Backups ==="
    find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort -r | head -n 3 || true

    echo
    echo "=== Configuration (no secrets) ==="
    grep -E '^(ENV|LOG_LEVEL|HTTP_PORT|HTTP_BIND_ADDRESS|DOMAIN|POSTGRES_HOST|POSTGRES_DB|ALLOWED_MODELS|ARCHIVE_ENABLED|WARMUP_ENABLED)=' \
        "${APP_DIR}/.env" 2>/dev/null || true
}

cmd_logs() {
    user_exists || die "Not installed."
    compose logs --tail=200 -f "$@"
}

stop_user_processes() {
    local uid="$1"

    as_app systemctl --user disable --now docker.service >/dev/null 2>&1 || true
    as_app dockerd-rootless-setuptool.sh uninstall >/dev/null 2>&1 || true

    loginctl disable-linger "${APP_USER}" 2>/dev/null || true
    loginctl terminate-user "${APP_USER}" 2>/dev/null || true
    systemctl stop "user@${uid}.service" 2>/dev/null || true

    # Re-send the kill on every pass so processes spawned while others die are caught too.
    for _ in {1..20}; do
        pkill -KILL -u "${uid}" 2>/dev/null || true
        pgrep -u "${uid}" >/dev/null || return 0
        sleep 1
    done

    # STAT Z = zombie (its parent, PPID, must exit or reap it); D = blocked in the kernel, often on a FUSE mount.
    echo "Processes owned by ${APP_USER} survived SIGKILL:" >&2
    ps -o pid,ppid,stat,wchan:32,lstart,args -u "${uid}" >&2 || true
    die "Could not stop every ${APP_USER} process; see the list above."
}

remove_docker_packages() {
    log "Removing Docker packages, repository, and all rootful Docker data."
    apt-get purge -y "${DOCKER_PACKAGES[@]}" || true
    rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc
    rm -rf /var/lib/docker /var/lib/containerd /etc/docker
}

cmd_uninstall() {
    local assume_yes=0 arg
    for arg in "$@"; do
        case "${arg}" in
            --yes) assume_yes=1 ;;
            *) die "Unknown uninstall option: ${arg}" ;;
        esac
    done

    local remove_docker="${DOCKER_INSTALLED_BY_SCRIPT}"

    echo "This permanently removes:"
    echo "  - the ${APP_USER} user and ${APP_HOME} (checkout, .env, rootless Docker images, containers, and the database)"
    echo "  - ${CREDS_FILE}, ${STATE_DIR}, and every backup in ${BACKUP_ROOT}"
    ((remove_docker)) && echo "  - Docker, which this script installed, including ALL rootful Docker data on this host"
    if ((!assume_yes)); then
        confirm "Uninstall Claude Code Proxy?" n || die "Uninstall cancelled."
    fi

    if user_exists; then
        local uid
        uid="$(get_uid)"

        if docker_socket_ready && [[ -f "${OVERRIDE_FILE}" && -d "${APP_DIR}" ]]; then
            log "Stopping and removing the Compose stack."
            compose down --volumes --remove-orphans || true
        fi

        log "Stopping every process owned by ${APP_USER}."
        stop_user_processes "${uid}"

        crontab -r -u "${APP_USER}" 2>/dev/null || true
        find /tmp /var/tmp /dev/shm -xdev -user "${uid}" -delete 2>/dev/null || true

        log "Removing user ${APP_USER} and ${APP_HOME}."
        # userdel exits non-zero for harmless reasons (e.g. no mail spool); check the outcome instead.
        userdel --remove "${APP_USER}" 2>/dev/null || true
        user_exists && die "userdel could not remove ${APP_USER}."
        getent group "${APP_USER}" >/dev/null && groupdel "${APP_USER}"
        rm -f "/var/lib/systemd/linger/${APP_USER}"
    fi

    # userdel leaves files owned by the user's subordinate IDs behind.
    [[ -n "${APP_HOME}" && "${APP_HOME}" == /home/* && -e "${APP_HOME}" ]] && rm -rf -- "${APP_HOME}"
    sed -i "/^${APP_USER}:/d" /etc/subuid /etc/subgid 2>/dev/null || true

    if [[ -f "${APPARMOR_PROFILE}" ]] && grep -qxF "${APPARMOR_MARKER}" "${APPARMOR_PROFILE}"; then
        log "Removing the rootlesskit AppArmor profile this script added."
        apparmor_parser -R "${APPARMOR_PROFILE}" 2>/dev/null || true
        rm -f "${APPARMOR_PROFILE}"
    fi

    ((remove_docker)) && remove_docker_packages

    rm -f "${CREDS_FILE}"
    rm -rf -- "${STATE_DIR:?}" "${BACKUP_ROOT:?}"

    log "Uninstall complete."
    echo
    echo "Not touched: host proxy (Caddy), firewall, VPN, and generic packages (uidmap, slirp4netns, ...)."
    ((remove_docker)) || echo "Docker was not installed by this script and was left in place."
    return 0
}

usage() {
    cat <<EOF
Usage:
  sudo $0 install            Install, or update an existing installation in place
  sudo $0 update             Back up, pull ${REPO_BRANCH}, rebuild and restart
  sudo $0 backup             Dump the database and .env to ${BACKUP_ROOT}
  sudo $0 status             Show deployment, container and health status
  sudo $0 logs [service]     Follow container logs
  sudo $0 uninstall [--yes]  Remove everything this script created

uninstall removes everything this script created, including backups and, if
this script installed it, Docker. --yes skips the confirmation prompt.
EOF
}

main() {
    local command="${1:-}"
    [[ $# -gt 0 ]] && shift

    case "${command}" in
        install | update | backup | status | logs | uninstall) ;;
        -h | --help | help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 1
            ;;
    esac

    require_root
    cd /
    # Read-only commands may run alongside an install/update.
    [[ "${command}" == status || "${command}" == logs ]] || take_lock
    load_state
    validate_overrides

    case "${command}" in
        install) cmd_install ;;
        update) cmd_update ;;
        backup) cmd_backup ;;
        status) cmd_status ;;
        logs) cmd_logs "$@" ;;
        uninstall) cmd_uninstall "$@" ;;
    esac
}

main "$@"
