#!/usr/bin/env bash
#
# emaild installer.
#
# Brings up a fresh installation on a clean Linux host with Docker. The host
# needs no Python, no compilers, and no copy of the source repository (§25).
#
#   ./install.sh --version 0.9.0-rc.1 [--dir /opt/emaild] [--lan] [--port 8000]
#
# Safe to re-run: it refuses to overwrite an existing configuration rather than
# silently replacing secrets (§19).
#
set -euo pipefail

readonly DEFAULT_DIR="/opt/emaild"
readonly DEFAULT_IMAGE="ghcr.io/soupnchill/emaild"
readonly MIN_DISK_MB=2048

err()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32m  ok\033[0m    %s\n' "$*"; }
info() { printf '%s\n' "$*"; }
die()  { err "$*"; exit 1; }

TARGET="$DEFAULT_DIR"
VERSION=""
LAN=0
PORT=8000

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)     TARGET="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --lan)     LAN=1; shift ;;
    --port)    PORT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 0. The installer's own files.
#
# install.sh is useless without compose.yaml (what to run) and appctl (how to
# operate it afterwards). Copying only some of deploy/ is the most likely way
# an install goes wrong -- an FTP client that transfers the file you clicked on
# is enough to do it -- and appctl used to be skipped SILENTLY, so the failure
# only surfaced at the end when the closing instructions said to run a file
# that was not there. Check before anything is created.
# ---------------------------------------------------------------------------

info "Checking the installer files..."

missing=""
for f in compose.yaml appctl; do
  [ -f "$SOURCE_DIR/$f" ] || missing="$missing $f"
done

if [ -n "$missing" ]; then
  err "these files are missing from $SOURCE_DIR:$missing"
  err ""
  err "install.sh needs every file in deploy/ -- compose.yaml describes the"
  err "services and appctl is how you operate them once they are running."
  err ""
  err "Copy the whole directory, not individual files. From a checkout:"
  err "    tar czf emaild-deploy.tar.gz deploy/"
  err "then on this host:"
  err "    tar xzf emaild-deploy.tar.gz && cd deploy"
  exit 1
fi
ok "compose.yaml and appctl present"

# ---------------------------------------------------------------------------
# 1-5. Preflight (§19 steps 1-5). Everything checked BEFORE anything is created.
# ---------------------------------------------------------------------------

info ""
info "Checking the host..."

[ "$(uname -s)" = "Linux" ] || die "this installer supports Linux only (found $(uname -s))"
ok "linux $(uname -r)"

case "$(uname -m)" in
  x86_64|aarch64) ok "architecture $(uname -m)" ;;
  *) die "unsupported architecture $(uname -m). Only amd64 and arm64 images are published." ;;
esac

command -v docker >/dev/null || die "docker is not installed. See https://docs.docker.com/engine/install/"
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

docker compose version >/dev/null 2>&1 \
  || die "docker compose v2 is not available. The 'docker-compose' v1 script will not work."
ok "compose $(docker compose version --short)"

docker info >/dev/null 2>&1 \
  || die "cannot talk to the docker daemon. Is it running, and is your user in the docker group?"
ok "docker daemon reachable"

parent="$(dirname "$TARGET")"
mkdir -p "$parent" 2>/dev/null || die "cannot create $parent -- try sudo, or pick another --dir"
avail="$(df -Pm "$parent" | awk 'NR==2{print $4}')"
[ "${avail:-0}" -ge "$MIN_DISK_MB" ] \
  || die "only ${avail} MB free at $parent; need at least ${MIN_DISK_MB} MB"
ok "disk ${avail} MB available"

port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE "[:.]${1}[[:space:]]"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | grep -qE "[:.]${1}[[:space:]]"
  else
    return 1  # cannot tell; let compose decide
  fi
}

if port_in_use "$PORT"; then
  err "port ${PORT} is already in use on this host."
  err ""
  err "Something else is listening there. Pick another with --port, e.g.:"
  err "    ./install.sh --version ${VERSION:-X.Y.Z} --port 8080${LAN:+ --lan}"
  exit 1
fi
ok "port ${PORT} available"

# ---------------------------------------------------------------------------
# 6. Installation directory -- refuse to clobber (§19: safe to re-run)
# ---------------------------------------------------------------------------

if [ -f "$TARGET/.env" ]; then
  err "an installation already exists at $TARGET"
  err ""
  err "Its .env holds the mailbox encryption key. Overwriting it would make every"
  err "stored SMTP credential undecryptable, and no upgrade needs that."
  err ""
  err "To upgrade:  cd $TARGET && ./appctl stop && edit EMAILD_VERSION && ./appctl start"
  err "To replace:  back it up first, then remove $TARGET by hand."
  exit 1
fi

# A fresh install must never silently adopt an existing database. Compose
# reuses any volume matching the project name, so a "clean" install onto a host
# that has one would come up pointing at somebody else's data -- with the new
# password, which fails confusingly, or the old one, which is worse.
if docker volume inspect emaild_postgres_data >/dev/null 2>&1; then
  err "a database volume named 'emaild_postgres_data' already exists on this host."
  err ""
  err "A fresh install would adopt it rather than start clean. That volume may"
  err "hold a previous installation's messages, keys, and suppression list."
  err ""
  err "  Inspect it : docker volume inspect emaild_postgres_data"
  err "  Keep it    : reinstall over the original directory instead of a new one"
  err "  Discard it : docker volume rm emaild_postgres_data   (IRREVERSIBLE)"
  exit 1
fi
ok "no conflicting database volume"

mkdir -p "$TARGET"
ok "installation directory $TARGET"

# ---------------------------------------------------------------------------
# 7-9. Configuration, secrets, identity
# ---------------------------------------------------------------------------

info ""
info "Generating configuration..."

# Generated in the container, so the host needs no Python (§25 criterion 3).
gen_fernet() {
  docker run --rm python:3.12-slim python -c \
    "from base64 import urlsafe_b64encode; import os; print(urlsafe_b64encode(os.urandom(32)).decode())"
}
gen_password() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

DB_PASSWORD="$(gen_password)"
ENCRYPTION_KEY="$(gen_fernet)"
DASHBOARD_TOKEN="$(gen_password)"

if [ "$LAN" -eq 1 ]; then
  BIND="0.0.0.0"
  info ""
  warn "LAN mode: the API will be reachable from your whole network over plain HTTP."
  warn "API keys travel in the Authorization header in CLEARTEXT and are readable"
  warn "by anything else on that network. On a trusted home LAN that is usually"
  warn "accepted -- but it should be a decision, not a surprise."
  info ""
  info "  A dashboard password has been generated because of this. For HTTPS and"
  info "  reachability beyond the LAN, enable the cloudflared profile later."
  info ""
else
  BIND="127.0.0.1"
fi

if [ -z "$VERSION" ]; then
  die "--version is required. Production must pin an exact version, never 'latest' (§8)."
fi

cat > "$TARGET/.env" <<ENVFILE
# emaild configuration -- generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
#
# This file contains two irreplaceable secrets. Mode 600. Back it up SEPARATELY
# from your database archives -- see backup-and-restore documentation.

EMAILD_IMAGE=$DEFAULT_IMAGE
EMAILD_VERSION=$VERSION

POSTGRES_USER=emaild
POSTGRES_DB=emaild
POSTGRES_PASSWORD=$DB_PASSWORD

# Where the API listens. 127.0.0.1 = this host only; 0.0.0.0 = the whole LAN.
EMAILD_BIND=$BIND
API_HOST_PORT=$PORT
EMAILD_LOG_LEVEL=INFO

# IRREPLACEABLE. Encrypts every mailbox SMTP password.
# Never regenerate this during an upgrade -- doing so makes all stored
# credentials undecryptable. Back it up somewhere other than this machine.
EMAILD_MAILBOX_ENCRYPTION_KEY=$ENCRYPTION_KEY

# Dashboard. Any username, this token as the password.
EMAILD_DASHBOARD_ENABLED=true
EMAILD_DASHBOARD_TOKEN=$DASHBOARD_TOKEN
EMAILD_DASHBOARD_BEHIND_PROXY_AUTH=false

# Set only when running the cloudflared profile.
CLOUDFLARE_TUNNEL_TOKEN=

# MXRoute control-plane credentials. Account-root: this key can delete mailboxes
# and manage reseller users. Required only for provisioning, which runs as
# role=admin and is never publicly routed. Fill in before adding a domain.
EMAILD_MXROUTE_SERVER=
EMAILD_MXROUTE_USERNAME=
EMAILD_MXROUTE_API_KEY=
ENVFILE

chmod 600 "$TARGET/.env"
ok "configuration written (mode 600)"
ok "database password generated"
ok "mailbox encryption key generated"
[ "$LAN" -eq 1 ] && ok "dashboard password generated"

cp "$SOURCE_DIR/compose.yaml" "$TARGET/compose.yaml"
cp "$SOURCE_DIR/appctl" "$TARGET/appctl"
# Set explicitly rather than inherited: a file that arrived over FTP or in a
# zip has usually lost its executable bit, and presence was already checked
# above.
chmod +x "$TARGET/appctl"
ok "appctl installed"

# ---------------------------------------------------------------------------
# 10-13. Pull, initialise, start
# ---------------------------------------------------------------------------

info ""
cd "$TARGET"
# Skip the pull when the exact image is already present. Correct for an
# air-gapped or pre-loaded host, and it keeps the installer honest about
# pinning: a missing local image still has to come from the registry.
if docker image inspect "${DEFAULT_IMAGE}:${VERSION}" >/dev/null 2>&1; then
  ok "image ${VERSION} already present locally"
else
  info "Pulling ${DEFAULT_IMAGE}:${VERSION}..."
  docker compose -f compose.yaml pull --quiet 2>&1 | tail -2 \
    || die "could not pull the image. Is the tag correct, and are you logged in to the registry?"
  ok "image pulled"
fi

DIGEST="$(docker image inspect "${DEFAULT_IMAGE}:${VERSION}" \
  --format '{{index .RepoDigests 0}}' 2>/dev/null || echo "local build (no registry digest)")"
info "  digest: $DIGEST"

info ""
info "Starting services (migrations run first)..."
docker compose -f compose.yaml up -d

# ---------------------------------------------------------------------------
# 14. Health
# ---------------------------------------------------------------------------

info ""
info "Waiting for health..."
healthy=0
for _ in $(seq 1 40); do
  sleep 3
  if curl -fsS "http://127.0.0.1:${PORT}/health/ready" 2>/dev/null | grep -q '"status":"ready"'; then
    healthy=1; break
  fi
done

if [ "$healthy" -ne 1 ]; then
  err "the API did not become healthy. Diagnose with:"
  err "    cd $TARGET && ./appctl doctor"
  err "    cd $TARGET && docker compose logs"
  exit 1
fi
ok "api ready"

INSTALL_ID="$(docker compose -f compose.yaml exec -T postgres \
  psql -U emaild -d emaild -tAc 'SELECT installation_id FROM installation LIMIT 1' 2>/dev/null | tr -d '\r ' || echo unknown)"

# ---------------------------------------------------------------------------
# 15-17. Report
# ---------------------------------------------------------------------------

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
URL="http://127.0.0.1:${PORT}"
[ "$LAN" -eq 1 ] && [ -n "$HOST_IP" ] && URL="http://${HOST_IP}:${PORT}"

cat <<REPORT

────────────────────────────────────────────────────────────────
emaild is running.

  version        $VERSION
  installation   $INSTALL_ID
  directory      $TARGET
  dashboard      $URL
  API base URL   $URL

REPORT

if [ "$LAN" -eq 1 ]; then
  cat <<REPORT
  Dashboard login: any username, password below.

      $DASHBOARD_TOKEN

REPORT
fi

cat <<'REPORT'
BEFORE YOU SEND ANYTHING

  1. Fill in the MXROUTE_* values in .env, then add a domain:
         ./appctl stop && nano .env && ./appctl start
         ./appctl admin domains token
  2. Publish the DNS records it prints, then verify:
         ./appctl admin domains verify

BACK THIS UP TODAY, NOT LATER

  Two things, stored in two places:

    a) ./appctl backup          → an archive; keep it on ANOTHER machine
    b) EMAILD_MAILBOX_ENCRYPTION_KEY from .env → a password manager

  The archive deliberately does NOT contain the key. Losing the key alone is
  recoverable (re-provision every mailbox). Losing the archive is not — message
  history and the suppression list cannot be rebuilt from anywhere.

  A backup that has never been restored is unproven. Test yours:
      ./appctl restore backups/<archive>.tar.gz

CHECK IT

      ./appctl doctor
────────────────────────────────────────────────────────────────
REPORT
