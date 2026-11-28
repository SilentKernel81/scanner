# Standalone System Tray Icon (`packaging/tray/`)

Adds a Linux system tray icon when running the standalone desktop program (AppImage or `launch.py`),
with **Open Card Scanner** (also left-click), **Restart**, and **Quit** actions.

## Why these files live here (syncing with `scanner-server`)

This repository (`scanner`) syncs from `scanner-server` via `git merge server/main`.
All tray code lives in standalone-only paths that `scanner-server` does not have:

- `packaging/tray/dbus_sni.py` — pure-Python D-Bus `org.kde.StatusNotifierItem` + `com.canonical.dbusmenu` implementation (zero external dependencies)
- `packaging/tray/launch.py` — supervises `app.py --app` and serves the tray icon
- `packaging/tray/bundle.sh` — copies `launch.py` and `dbus_sni.py` into `AppDir/usr/tray/` during `scripts/build-appimage.sh`
- `tests/tray/test_tray.py` — unit tests for the wire protocol, menu layout, and supervisor lifecycle

Only two single-line conditional hooks exist in shared files:
1. `packaging/AppRun` — invokes `$HERE/usr/tray/launch.py` when present
2. `scripts/build-appimage.sh` — runs `packaging/tray/bundle.sh` when present

Because `git merge server/main` only modifies files changed in `scanner-server`, none of the files under
`packaging/tray/` or `tests/tray/` are ever erased on sync.

## How it works

- Connects directly to the user's session D-Bus (`$DBUS_SESSION_BUS_ADDRESS`) using the standard
  StatusNotifierItem + DBusMenu protocols — works on Wayland and X11 (Hyprland/Waybar, KDE Plasma,
  GNOME with AppIndicator, XFCE, Cinnamon, Sway, Quickshell/Omarchy) with no GTK, Qt, or X11 libraries.
  **Tested only on Omarchy (Hyprland), 2026-11-27**: the icon registers, and Restart and Quit work
  (triggered over D-Bus with `busctl`). The other desktops in this list were never tried.
- Renders the Card Scanner icon via both `IconName` + `IconThemePath` (`mtg-scanner.svg`) and
  `IconPixmap` (32x32 and 64x64 big-endian ARGB32 fallback).
- Re-registers automatically if the panel's `StatusNotifierWatcher` restarts.
- Fails silently (no crash) on headless machines without a session D-Bus.
- Pass `--no-tray` to disable the tray icon.

## Testing

```bash
# Unit tests
venv/bin/python -m unittest discover -s tests/tray -t tests/tray   # also run by: unittest discover tests

# Run directly from a checkout against a temporary data folder
MTG_SCANNER_HOME=/tmp/try-tray venv/bin/python packaging/tray/launch.py --no-browser
```
