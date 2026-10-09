#!/bin/bash
# ============================================================================
# Builds dist/Card_Scanner-<arch>.AppImage: the app with its own Python, Python packages,
# Node.js and the light-ocr reader - nothing to install on the computer that runs it
# (v4l2-ctl from v4l-utils is still used when present: focus lock and sweeps).
#
# Needs uv, curl and tar; downloads Node.js and appimagetool into build/appimage/cache once.
# The parts are portable builds (python-build-standalone, manylinux wheels, nodejs.org), so
# the result does not depend on this computer's glibc.
#
# Run it:  MTG_SCANNER_HOME=/tmp/try dist/Card_Scanner-x86_64.AppImage --no-browser
# (without MTG_SCANNER_HOME the data goes to ~/.local/share/mtg-scanner)
# ============================================================================

set -euo pipefail

PYTHON_VERSION=3.12
NODE_LINE=latest-v22.x   # light-ocr needs Node.js 22 or newer

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build/appimage"
CACHE="$BUILD/cache"
APPDIR="$BUILD/AppDir"
APP="$APPDIR/usr/app"
ARCH="$(uname -m)"

case "$ARCH" in
    x86_64)  NODE_ARCH=x64 ;;
    aarch64) NODE_ARCH=arm64 ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

step() { echo; echo "== $1"; }
for program in uv curl tar git; do
    command -v "$program" > /dev/null || { echo "Missing: $program" >&2; exit 1; }
done

rm -rf "$APPDIR"
mkdir -p "$APP/bin" "$CACHE" "$ROOT/dist"

step "Python $PYTHON_VERSION"
uv python install "$PYTHON_VERSION"
PYTHON_HOME="$(dirname "$(dirname "$(readlink -f "$(uv python find --system --python-preference only-managed "$PYTHON_VERSION")")")")"
cp -a "$PYTHON_HOME" "$APPDIR/usr/python"
PYTHON="$APPDIR/usr/python/bin/python3"
# What a web app never loads
LIB="$APPDIR/usr/python/lib"
rm -rf "$APPDIR/usr/python/include" "$APPDIR/usr/python/share" \
       "$LIB"/python*/{test,idlelib,tkinter,turtledemo,ensurepip} \
       "$LIB"/{tcl,tk,itcl,thread}* "$LIB"/lib{tcl,tk}*
"$PYTHON" --version

step "Python packages"
uv pip install --python "$PYTHON" --break-system-packages --link-mode copy --compile-bytecode \
    -r "$ROOT/requirements.txt"

step "The app"
# What git knows or would add (not data/, venv/, node_modules/), without docs and scripts
(cd "$ROOT" && git ls-files --cached --others --exclude-standard -- \
    '*.py' config.yaml LICENSE templates static games ocr \
    | while read -r file; do [ -f "$file" ] && cp --parents "$file" "$APP/"; done)
"$PYTHON" -m compileall -q "$APP"

step "Node.js and light-ocr"
sums="$(curl -fsSL "https://nodejs.org/dist/$NODE_LINE/SHASUMS256.txt")"
line="$(grep "linux-$NODE_ARCH.tar.xz\$" <<< "$sums")"
node_file="${line##* }"
[ -f "$CACHE/$node_file" ] || curl -fL --progress-bar -o "$CACHE/$node_file" "https://nodejs.org/dist/$NODE_LINE/$node_file"
echo "${line%% *}  $CACHE/$node_file" | sha256sum -c -
rm -rf "$BUILD/node" && mkdir "$BUILD/node"
tar -xJf "$CACHE/$node_file" -C "$BUILD/node" --strip-components=1
cp "$BUILD/node/bin/node" "$APP/bin/node"
(cd "$APP/ocr" && PATH="$BUILD/node/bin:$PATH" npm ci --omit=dev --no-audit --no-fund)
"$APP/bin/node" --version

step "AppImage"
cp "$ROOT/packaging/AppRun" "$ROOT/packaging/mtg-scanner.desktop" "$ROOT/packaging/mtg-scanner.svg" "$APPDIR/"
chmod +x "$APPDIR/AppRun"
ln -sf mtg-scanner.svg "$APPDIR/.DirIcon"
if command -v appimagetool > /dev/null; then
    TOOL=(appimagetool)
else
    tool_file="$CACHE/appimagetool-$ARCH.AppImage"
    [ -f "$tool_file" ] || curl -fL --progress-bar -o "$tool_file" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$tool_file"
    TOOL=("$tool_file" --appimage-extract-and-run)  # works without FUSE
fi
OUTPUT="$ROOT/dist/Card_Scanner-$ARCH.AppImage"
ARCH="$ARCH" "${TOOL[@]}" "$APPDIR" "$OUTPUT"

echo
echo "✓ $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
