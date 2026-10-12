#!/usr/bin/env bash
# ============================================================================
# MTG Card Scanner - deployment script
#
# Run it on the machine that runs the scanner (Raspberry Pi or any Linux PC). It sets up the
# two programs a single machine needs: the server (app.py) and, for the camera plugged into
# this machine, a camera station (station_client.py) that sends its cards to that server.
#   ./scripts/deploy.sh                  # install / repair
#   ./scripts/deploy.sh --service        # ... and run both as systemd services
#   ./scripts/deploy.sh --update         # pull the latest code, update, restart
# Remotely:  ssh pi@scanner.local 'cd scanner && ./scripts/deploy.sh --update'
#
# Safe to run again at any time: it only does what is missing.
# ============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="mtg-scanner"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
# The camera on this machine: a station of its own, connecting to the server above
STATION_SERVICE_NAME="mtg-scanner-station"
STATION_SERVICE_FILE="/etc/systemd/system/${STATION_SERVICE_NAME}.service"
MIN_PYTHON="3.10"

INSTALL_SERVICE=0
REMOVE_SERVICE=0
UPDATE=0
PICAMERA=0
SKIP_DATABASE=0
REFRESH_CARDS=0
CAMERA_INDEX=""

if [ -t 1 ]; then
    BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; RED=''; BLUE=''; NC=''
fi
step() { echo -e "\n${BLUE}${BOLD}==> $*${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
die()  { echo -e "  ${RED}✗ $*${NC}" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: $0 [options]

Installs or updates the MTG Card Scanner in ${DIR}.

Options:
  --service           Install and start the systemd services - the server and this machine's
                      camera station (both start on boot)
  --remove-service    Stop and remove the systemd services
  --update            Pull the latest code (git) before installing; restarts the services
  --camera N          Use USB camera /dev/videoN (sets camera.usb_index in config.yaml)
  --picamera          Raspberry Pi camera module (installs python3-picamera2)
  --skip-database     Don't download the card database
  --refresh-cards     Re-download the card database (latest cards and prices)
  -h, --help          Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --service) INSTALL_SERVICE=1 ;;
        --remove-service) REMOVE_SERVICE=1 ;;
        --update) UPDATE=1 ;;
        --camera) [ $# -ge 2 ] || die "--camera needs a number"; CAMERA_INDEX="$2"; shift ;;
        --picamera) PICAMERA=1 ;;
        --skip-database) SKIP_DATABASE=1 ;;
        --refresh-cards) REFRESH_CARDS=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; die "Unknown option: $1" ;;
    esac
    shift
done
[ -z "$CAMERA_INDEX" ] || [[ "$CAMERA_INDEX" =~ ^[0-9]+$ ]] || die "--camera needs a number, e.g. --camera 2"

cd "$DIR"
[ -f app.py ] && [ -f requirements.txt ] || die "Run this script from the scanner project (scripts/deploy.sh)"
[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (sudo is used only where needed)"

SUDO=""
sudo_cmd() {
    if [ -z "$SUDO" ]; then
        command -v sudo >/dev/null || die "sudo is needed for: $*"
        SUDO="sudo"
    fi
    $SUDO "$@"
}

service_installed() { [ -f "$SERVICE_FILE" ]; }

# ----------------------------------------------------------------------------
if [ "$REMOVE_SERVICE" -eq 1 ]; then
    step "Removing the ${SERVICE_NAME} service"
    if service_installed; then
        sudo_cmd systemctl disable --now "$STATION_SERVICE_NAME" 2>/dev/null || true
        sudo_cmd systemctl disable --now "$SERVICE_NAME" || true
        sudo_cmd rm -f "$SERVICE_FILE" "$STATION_SERVICE_FILE"
        sudo_cmd systemctl daemon-reload
        ok "Services removed (your data in ${DIR}/data is untouched)"
    else
        ok "No service installed"
    fi
    exit 0
fi

# ----------------------------------------------------------------------------
if [ "$UPDATE" -eq 1 ]; then
    step "Updating the code"
    [ -d .git ] || die "--update needs a git checkout (git clone the project)"
    before=$(git rev-parse --short HEAD)
    # --autostash keeps local edits (e.g. your camera index in config.yaml)
    git pull --ff-only --autostash || die "git pull failed - resolve it (git status) and run again"
    after=$(git rev-parse --short HEAD)
    if [ "$before" = "$after" ]; then ok "Already up to date ($after)"; else ok "Updated $before -> $after"; fi
fi

# ----------------------------------------------------------------------------
step "System packages"

version_ok() {  # version_ok <python> : python >= MIN_PYTHON
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= tuple(map(int, '$MIN_PYTHON'.split('.'))) else 1)" 2>/dev/null
}

PM=""
for candidate in apt-get pacman dnf zypper; do
    if command -v "$candidate" >/dev/null; then PM="$candidate"; break; fi
done

packages=()
command -v python3 >/dev/null || packages+=("python3")
if command -v python3 >/dev/null && ! python3 -c "import ensurepip, venv" 2>/dev/null; then
    packages+=("python3-venv")          # Debian / Raspberry Pi OS ship venv separately
fi
command -v v4l2-ctl >/dev/null || packages+=("v4l-utils")   # USB camera focus/zoom controls
if [ "$PICAMERA" -eq 1 ] && ! python3 -c "import picamera2" 2>/dev/null; then
    packages+=("python3-picamera2")
fi

if [ ${#packages[@]} -eq 0 ]; then
    ok "All required system packages are installed"
else
    case "$PM" in
        apt-get) names=("${packages[@]}") ;;
        pacman) names=(); for p in "${packages[@]}"; do case "$p" in python3|python3-venv) names+=("python") ;; python3-picamera2) die "The Pi camera module is only supported on Raspberry Pi OS" ;; *) names+=("$p") ;; esac; done ;;
        dnf|zypper) names=(); for p in "${packages[@]}"; do case "$p" in python3-venv) ;; python3-picamera2) die "The Pi camera module is only supported on Raspberry Pi OS" ;; *) names+=("$p") ;; esac; done ;;
        *) die "Please install: ${packages[*]} (no supported package manager found)" ;;
    esac
    echo "  Installing: ${names[*]}"
    case "$PM" in
        apt-get) sudo_cmd apt-get update -qq && sudo_cmd apt-get install -y -qq "${names[@]}" ;;
        pacman) sudo_cmd pacman -S --needed --noconfirm "${names[@]}" ;;
        dnf) sudo_cmd dnf install -y "${names[@]}" ;;
        zypper) sudo_cmd zypper install -y "${names[@]}" ;;
    esac
    ok "Installed ${names[*]}"
fi

# ----------------------------------------------------------------------------
step "Python environment"

HAVE_UV=0
command -v uv >/dev/null && HAVE_UV=1

venv_ok() {
    [ -x venv/bin/python ] && version_ok venv/bin/python || return 1
    if [ "$PICAMERA" -eq 1 ]; then venv/bin/python -c "import picamera2" 2>/dev/null || return 1; fi
    return 0
}

if venv_ok; then
    ok "Using existing venv ($(venv/bin/python --version))"
else
    [ -d venv ] && { warn "Replacing unusable venv"; rm -rf venv; }
    if [ "$PICAMERA" -eq 1 ]; then
        # picamera2 comes from apt, so the venv must see the system packages
        version_ok python3 || die "Python ${MIN_PYTHON}+ is required ($(python3 --version))"
        python3 -m venv --system-site-packages venv
    elif command -v python3 >/dev/null && version_ok python3; then
        python3 -m venv venv
    elif [ "$HAVE_UV" -eq 1 ]; then
        uv venv -q --python 3.12 venv
    else
        die "Python ${MIN_PYTHON}+ is required - install it or uv (https://docs.astral.sh/uv/)"
    fi
    ok "Created venv ($(venv/bin/python --version))"
fi

pip_install() {
    if [ -x venv/bin/pip ]; then
        venv/bin/python -m pip install -q --disable-pip-version-check "$@"
    elif [ "$HAVE_UV" -eq 1 ]; then
        uv pip install -q --python venv/bin/python "$@"
    else
        venv/bin/python -m ensurepip -q && venv/bin/python -m pip install -q --disable-pip-version-check "$@"
    fi
}

echo "  Installing Python packages (first run takes a few minutes)..."
pip_install -r requirements.txt
pip_install -r requirements-client.txt   # the camera station's (station_client.py)
venv/bin/python -c "import flask, flask_socketio, cv2, numpy, PIL, yaml, dotenv, requests" \
    || die "Python packages are not importable - see the errors above"
ok "Python packages installed"

# ----------------------------------------------------------------------------
step "OCR reader (optional)"

# light-ocr reads cards before the vision AI is asked (card_ocr.py); it runs on Node.js
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 \
        && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 22 ]; then
    if (cd ocr && npm install --no-audit --no-fund --silent); then
        ok "light-ocr installed (Settings > Read with OCR first)"
    else
        warn "light-ocr could not be installed - cards will be read by the vision AI only"
    fi
else
    warn "Node.js 22+ not found - cards will be read by the vision AI only"
    echo "    Install Node.js (with npm) and run this script again to read cards with OCR first"
fi

# ----------------------------------------------------------------------------
step "Configuration"

mkdir -p data scanned_cards
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    ok "Created .env from .env.example"
else
    ok ".env exists"
fi

config_get() {  # config_get dotted.key [default]   e.g. config_get camera.usb_index 0
    venv/bin/python -c 'import sys, yaml
value = yaml.safe_load(open("config.yaml")) or {}
for key in sys.argv[1].split("."):
    value = value.get(key) if isinstance(value, dict) else None
print(sys.argv[2] if value is None else value)' "$1" "${2:-}"
}

if [ -n "$CAMERA_INDEX" ]; then
    sed -i -E "s/^(  usb_index:)[[:space:]]*[0-9]+/\1 ${CAMERA_INDEX}/" config.yaml
    ok "Set camera.usb_index to ${CAMERA_INDEX}"
fi

provider=$(config_get vision_ai.provider gemini)
provider="${VISION_AI_PROVIDER:-$provider}"
if [ -f data/settings.json ]; then  # the provider chosen in the web UI wins
    provider=$(venv/bin/python -c "import json; print(json.load(open('data/settings.json')).get('ai_provider') or '$provider')")
fi
case "$provider" in
    local) ok "Vision AI: local model server ($(config_get vision_ai.local.endpoint))" ;;
    *)  key_var="$(echo "$provider" | tr '[:lower:]' '[:upper:]')_API_KEY"
        if grep -Eqs "^${key_var}=.+" .env data/api_keys.env; then
            ok "Vision AI: ${provider} (API key configured)"
        else
            warn "Vision AI: ${provider} - no API key yet: enter it in Settings -> Vision AI (or add ${key_var}=... to .env)"
        fi ;;
esac

# ----------------------------------------------------------------------------
step "Camera"

camera_type=$(config_get camera.type auto)
usb_index=$(config_get camera.usb_index 0)
if [ "$PICAMERA" -eq 1 ] || [ "$camera_type" = "picamera" ]; then
    ok "Raspberry Pi camera module (camera.type: ${camera_type})"
else
    if [ -e "/dev/video${usb_index}" ]; then
        name=$(cat "/sys/class/video4linux/video${usb_index}/name" 2>/dev/null || echo "unknown")
        ok "USB camera /dev/video${usb_index}: ${name}"
    else
        warn "No camera at /dev/video${usb_index} (camera.usb_index in config.yaml)"
    fi
    if command -v v4l2-ctl >/dev/null; then
        echo "  Cameras found:"
        v4l2-ctl --list-devices 2>/dev/null | sed 's/^/    /' || echo "    (none)"
        echo "  Pick one with: $0 --camera N"
    fi
fi
if [ -e "/dev/video${usb_index}" ] && [ ! -r "/dev/video${usb_index}" ]; then
    # Desktop sessions get camera access automatically; over SSH you need the video group
    warn "No permission to read /dev/video${usb_index} - the station's service gets access anyway; for manual runs: sudo usermod -aG video $(id -un) (then log in again)"
fi

# ----------------------------------------------------------------------------
step "Card database"

DB="data/cards_database.db"
if [ "$SKIP_DATABASE" -eq 1 ]; then
    warn "Skipped - download it later with: venv/bin/python setup_database.py"
elif [ -s "$DB" ] && [ "$REFRESH_CARDS" -eq 0 ]; then
    ok "Card database present ($(du -h "$DB" | cut -f1)) - refresh with --refresh-cards"
else
    echo "  Downloading card data from Scryfall (a few minutes)..."
    # setup_database.py asks before replacing an existing database
    echo y | venv/bin/python setup_database.py | grep -E "Total cards|Error|✓ Database" | sed 's/^/  /' || true
    [ -s "$DB" ] || die "Card database download failed - run: venv/bin/python setup_database.py"
    ok "Card database ready"
fi

# ----------------------------------------------------------------------------
# Does this machine have a camera of its own? Yes, unless the camera type is 'remote'
# (camera.type in config.yaml, or SCANNER_CAMERA in .env) - which until 2026-10-12 told the
# server "no camera here" and still means exactly that to this script and to start.sh: only
# the server is installed and started. Asked of config.py, so both scripts and the programs
# read the same value.
wants_station() {
    [ "$(venv/bin/python -c 'from config import Config; print(str(Config.CAMERA_TYPE).lower())' 2>/dev/null)" != "remote" ]
}

install_unit() {  # install_unit <name>: /etc/systemd/system/<name>.service from scripts/<name>.service
    local tmp; tmp=$(mktemp)
    sed -e "s|@USER@|$(id -un)|g" -e "s|@GROUP@|$(id -gn)|g" -e "s|@DIR@|${DIR}|g" \
        -e "s|@PORT@|$(config_get flask.port 5000)|g" "scripts/$1.service" > "$tmp"
    sudo_cmd install -m 644 "$tmp" "/etc/systemd/system/$1.service"
    rm -f "$tmp"
}

start_station_service() {  # install (or refresh), enable and start the camera's service, and say how it went
    mkdir -p data scanned_cards station_captures   # the unit names them as writable: they must exist
    install_unit "$STATION_SERVICE_NAME"
    sudo_cmd systemctl daemon-reload
    sudo_cmd systemctl enable -q "$STATION_SERVICE_NAME"
    sudo_cmd systemctl restart "$STATION_SERVICE_NAME"
    sleep 3
    if systemctl is-active -q "$STATION_SERVICE_NAME"; then
        ok "Service ${STATION_SERVICE_NAME} is running: this machine's camera (starts on boot)"
    else
        warn "The camera station did not start - see: journalctl -u ${STATION_SERVICE_NAME} -n 50 (the server runs without it)"
    fi
}

if [ "$INSTALL_SERVICE" -eq 1 ]; then
    step "System services"
    command -v systemctl >/dev/null || die "systemd is needed for --service"
    mkdir -p data scanned_cards
    install_unit "$SERVICE_NAME"
    sudo_cmd systemctl daemon-reload
    sudo_cmd systemctl enable -q "$SERVICE_NAME"
    sudo_cmd systemctl restart "$SERVICE_NAME"
    sleep 3
    if systemctl is-active -q "$SERVICE_NAME"; then
        ok "Service ${SERVICE_NAME} is running (starts on boot)"
    else
        die "Service failed to start - see: journalctl -u ${SERVICE_NAME} -n 50"
    fi
    # The camera on this machine is read by a second service, a station that sends its cards
    # to the server above (it is told the server's port; the rest it reads from config.yaml
    # and .env)
    if wants_station; then
        start_station_service
    else
        ok "No camera on this machine (camera.type: remote in config.yaml): the server only"
    fi
elif service_installed; then
    step "System services"
    if [ "$REFRESH_CARDS" -eq 1 ] && [ "$UPDATE" -eq 0 ]; then
        # New card data is read straight from the database file - no restart (and no sudo,
        # so this also works from cron)
        ok "${SERVICE_NAME} keeps running and uses the new card data"
    else
        sudo_cmd systemctl restart "$SERVICE_NAME"
        ok "Restarted ${SERVICE_NAME}"
        if [ -f "$STATION_SERVICE_FILE" ]; then
            sudo_cmd systemctl restart "$STATION_SERVICE_NAME"
            ok "Restarted ${STATION_SERVICE_NAME}"
        elif wants_station; then
            # A machine installed before 2026-10-12: its one service, the server, read the
            # camera itself. The server no longer does, so an update that only restarted it
            # would leave a scanner that worked an hour ago unable to scan, also after the
            # next reboot. The camera's service is added here, as part of the update.
            warn "This machine's camera was read by the server, which no longer opens one: adding its own service"
            start_station_service
        fi
    fi
fi

# ----------------------------------------------------------------------------
step "Done"

port=$(config_get flask.port 5000)
echo "  Open the scanner at:"
echo "    http://localhost:${port}"
for ip in $(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1); do
    echo "    http://${ip}:${port}"
done
echo
if service_installed; then
    if [ -f "$STATION_SERVICE_FILE" ]; then
        echo "  Services: sudo systemctl {status|restart|stop} ${SERVICE_NAME} ${STATION_SERVICE_NAME}"
        echo "  Logs:     journalctl -u ${SERVICE_NAME} -f      (the camera: -u ${STATION_SERVICE_NAME})"
    else
        echo "  Service:  sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
        echo "  Logs:     journalctl -u ${SERVICE_NAME} -f"
    fi
else
    echo "  Start it: cd ${DIR} && ./scripts/start.sh     (the server and this machine's camera)"
    echo "  or the two by hand, each in its own terminal:"
    echo "            venv/bin/python app.py"
    echo "            venv/bin/python station_client.py --server http://localhost:${port}"
    echo "  Or run both as services (start on boot): $0 --service"
fi
if wants_station; then
    echo "  Camera:   which one, and its name: http://localhost:8080 (the station's setup page)"
fi
echo "  Update:   $0 --update"
