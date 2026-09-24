#!/usr/bin/env bash
# ============================================================================
# MTG Card Scanner - deployment script
#
# Run it on the machine that runs the scanner (Raspberry Pi or any Linux PC):
#   ./scripts/deploy.sh                  # install / repair
#   ./scripts/deploy.sh --service        # ... and run it as a systemd service
#   ./scripts/deploy.sh --update         # pull the latest code, update, restart
# Remotely:  ssh pi@scanner.local 'cd scanner && ./scripts/deploy.sh --update'
#
# Safe to run again at any time: it only does what is missing.
# ============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="mtg-scanner"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
MIN_PYTHON="3.10"

INSTALL_SERVICE=0
REMOVE_SERVICE=0
UPDATE=0
WITH_YOLO=0
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
  --service           Install and start a systemd service (starts on boot)
  --remove-service    Stop and remove the systemd service
  --update            Pull the latest code (git) before installing; restarts the service
  --camera N          Use USB camera /dev/videoN (sets camera.usb_index in config.yaml)
  --picamera          Raspberry Pi camera module (installs python3-picamera2)
  --with-yolo         Also install the optional YOLO fallback detector (~1 GB)
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
        --with-yolo) WITH_YOLO=1 ;;
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
        sudo_cmd systemctl disable --now "$SERVICE_NAME" || true
        sudo_cmd rm -f "$SERVICE_FILE"
        sudo_cmd systemctl daemon-reload
        ok "Service removed (your data in ${DIR}/data is untouched)"
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
    elif [ "$WITH_YOLO" -eq 1 ] && [ "$HAVE_UV" -eq 1 ]; then
        uv venv -q --python 3.12 venv      # PyTorch wheels can lag the newest Python
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
if [ "$WITH_YOLO" -eq 1 ]; then
    if [ "$(uname -m)" = "x86_64" ] && ! venv/bin/python -c "import torch" 2>/dev/null; then
        # CPU-only PyTorch: far smaller than the default CUDA build
        pip_install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    fi
    pip_install -r requirements-yolo.txt
fi
venv/bin/python -c "import flask, flask_socketio, cv2, numpy, PIL, yaml, dotenv, requests" \
    || die "Python packages are not importable - see the errors above"
ok "Python packages installed$([ "$WITH_YOLO" -eq 1 ] && echo " (with YOLO)")"

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
        if grep -Eq "^${key_var}=.+" .env; then
            ok "Vision AI: ${provider} (API key found in .env)"
        else
            warn "Vision AI: ${provider} - add ${key_var}=... to ${DIR}/.env (or pick a local model in Settings)"
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
    warn "No permission to read /dev/video${usb_index} - the service gets access anyway; for manual runs: sudo usermod -aG video $(id -un) (then log in again)"
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
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    step "System service"
    command -v systemctl >/dev/null || die "systemd is needed for --service"
    tmp=$(mktemp)
    sed -e "s|@USER@|$(id -un)|g" -e "s|@GROUP@|$(id -gn)|g" -e "s|@DIR@|${DIR}|g" scripts/mtg-scanner.service > "$tmp"
    sudo_cmd install -m 644 "$tmp" "$SERVICE_FILE"
    rm -f "$tmp"
    sudo_cmd systemctl daemon-reload
    sudo_cmd systemctl enable -q "$SERVICE_NAME"
    sudo_cmd systemctl restart "$SERVICE_NAME"
    sleep 3
    if systemctl is-active -q "$SERVICE_NAME"; then
        ok "Service ${SERVICE_NAME} is running (starts on boot)"
    else
        die "Service failed to start - see: journalctl -u ${SERVICE_NAME} -n 50"
    fi
elif service_installed; then
    step "System service"
    if [ "$REFRESH_CARDS" -eq 1 ] && [ "$UPDATE" -eq 0 ]; then
        # New card data is read straight from the database file - no restart (and no sudo,
        # so this also works from cron)
        ok "${SERVICE_NAME} keeps running and uses the new card data"
    else
        sudo_cmd systemctl restart "$SERVICE_NAME"
        ok "Restarted ${SERVICE_NAME}"
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
    echo "  Service:  sudo systemctl {status|restart|stop} ${SERVICE_NAME}"
    echo "  Logs:     journalctl -u ${SERVICE_NAME} -f"
else
    echo "  Start it: cd ${DIR} && venv/bin/python app.py"
    echo "  Or run it as a service (starts on boot): $0 --service"
fi
echo "  Update:   $0 --update"
