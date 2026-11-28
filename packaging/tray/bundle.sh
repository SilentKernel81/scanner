#!/bin/bash
# Bundles the standalone system tray launcher into an AppDir (called by scripts/build-appimage.sh).
# Standalone-only file: lives under packaging/tray/ so syncing from scanner-server never touches it.
set -euo pipefail

APPDIR="$1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$APPDIR/usr/tray"
cp "$HERE/launch.py" "$HERE/dbus_sni.py" "$APPDIR/usr/tray/"
chmod +x "$APPDIR/usr/tray/launch.py"
"$APPDIR/usr/python/bin/python3" -m compileall -q "$APPDIR/usr/tray"
