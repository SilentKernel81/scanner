#!/usr/bin/env python3
"""
Standalone AppImage launcher with a Linux system tray icon (Open, Restart, Quit).

Started by packaging/AppRun (or directly from a checkout: venv/bin/python packaging/tray/launch.py).
Runs `app.py --app` as a supervised child process so that:
  - Open opens the Card Scanner web UI in the browser (also on tray left-click)
  - Restart cleanly stops the server (SIGTERM -> camera + DB cleanup) and starts it again
    without opening a duplicate browser tab
  - Quit cleanly stops the server (works even if a settings password is set) and exits
  - A second launch while already running lets app.py open the existing instance's browser
    tab and exits without showing a duplicate tray icon
  - Pass-through commands (--quit, --support-bundle, -h, --help) bypass the tray completely

Standalone-only file (lives under packaging/tray/ so syncing from scanner-server never touches it).
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

# Allow importing dbus_sni from beside this file without touching card_scanner's package layout
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from dbus_sni import MenuItem, TrayIcon  # noqa: E402

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')
PASSTHROUGH_FLAGS = {'--quit', '--support-bundle', '-h', '--help'}
STOP_TIMEOUT_SECONDS = 15.0


def find_app_paths() -> tuple[Path, Path]:
    """Return `(app_dir, icon_dir)` for either an AppImage or a repository checkout."""
    appdir = os.environ.get('APPDIR')
    if appdir:
        base = Path(appdir).resolve()
        candidate = base / 'usr' / 'app'
        if (candidate / 'app.py').is_file():
            return candidate, base
    # Checkout layout: <repo>/packaging/tray/launch.py -> repo root is HERE.parent.parent
    repo = HERE.parent.parent
    return repo, repo / 'packaging'


def resolve_user_dir(app_dir: Path) -> Path:
    """Mirror card_scanner.paths._user_dir() without importing the full server package."""
    override = os.environ.get('MTG_SCANNER_HOME')
    if override:
        return Path(override).expanduser().absolute()
    appdir = os.environ.get('APPDIR')
    if appdir and Path(appdir).resolve() in app_dir.resolve().parents:
        return Path.home() / '.mtg-scanner'
    return app_dir


def _load_yaml_flask(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        return data.get('flask') or {} if isinstance(data, dict) else {}
    except Exception:
        return {}


def compute_local_url(app_dir: Path, user_dir: Path) -> tuple[str, str, int]:
    """
    Compute `(url, connect_host, port)` matching card_scanner.server.program.local_url()
    for `--app` mode, reading config.yaml (with optional user overlay) and data/settings.json.
    """
    flask_cfg = _load_yaml_flask(app_dir / 'config.yaml')
    if user_dir != app_dir:
        flask_cfg.update(_load_yaml_flask(user_dir / 'config.yaml'))

    host_cfg = str(flask_cfg.get('host', 'auto'))
    try:
        port = int(flask_cfg.get('port', 5000))
    except (TypeError, ValueError):
        port = 5000

    saved_network = None
    settings_file = user_dir / 'data' / 'settings.json'
    if settings_file.is_file():
        try:
            saved = json.loads(settings_file.read_text(encoding='utf-8'))
            if isinstance(saved, dict) and 'network_access' in saved and saved['network_access'] is not None:
                saved_network = bool(saved['network_access'])
        except Exception:
            pass

    if saved_network is not None:
        net = saved_network
    elif host_cfg == 'auto':
        net = False  # --app mode defaults to this computer only
    else:
        net = host_cfg not in LOCAL_HOSTS

    if not net:
        listen_host = '127.0.0.1'
    elif host_cfg == 'auto' or host_cfg in LOCAL_HOSTS:
        listen_host = '0.0.0.0'
    else:
        listen_host = host_cfg

    connect_host = 'localhost' if listen_host in ('0.0.0.0', '', '::', '127.0.0.1', 'localhost', '::1') else listen_host
    url = f"http://[{connect_host}]:{port}" if ':' in connect_host else f"http://{connect_host}:{port}"
    return url, connect_host, port


def is_lock_held(user_dir: Path) -> bool:
    """Return True if another Card Scanner instance currently holds data/app.lock."""
    if fcntl is None:
        return False
    lock_file = user_dir / 'data' / 'app.lock'
    if not lock_file.is_file():
        return False
    try:
        with open(lock_file, 'r+') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def port_answers(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


class Supervisor:
    """Supervises `app.py --app` and exposes Open / Restart / Quit via a system tray icon."""

    def __init__(self, app_dir: Path, icon_dir: Path, passthrough_args: list[str], use_tray: bool = True) -> None:
        self.app_dir = app_dir
        self.icon_dir = icon_dir
        self.user_dir = resolve_user_dir(app_dir)
        self.passthrough_args = list(passthrough_args)
        self.use_tray = use_tray

        # Re-entrant: the SIGINT / SIGTERM handler calls request_quit on the main thread, which may
        # already hold the lock inside run()
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._restart_requested = False
        self._quit_requested = False
        self._wake = threading.Event()
        self._tray: TrayIcon | None = None

    def _command(self, extra_args: list[str] | None = None) -> list[str]:
        cmd = [sys.executable, '-B', str(self.app_dir / 'app.py'), '--app'] + list(self.passthrough_args)
        for arg in extra_args or ():
            if arg not in cmd:
                cmd.append(arg)
        return cmd

    def open_browser(self) -> None:
        url, _, _ = compute_local_url(self.app_dir, self.user_dir)
        webbrowser.open(url)

    def request_restart(self) -> None:
        with self._lock:
            if self._quit_requested:
                return
            self._restart_requested = True
            proc = self._process
        if self._tray is not None:
            self._tray.set_tooltip('Restarting...')
        self._terminate_child(proc)
        self._wake.set()

    def request_quit(self) -> None:
        with self._lock:
            self._quit_requested = True
            self._restart_requested = False
            proc = self._process
        if self._tray is not None:
            self._tray.set_tooltip('Stopping...')
        self._terminate_child(proc)
        self._wake.set()

    @staticmethod
    def _terminate_child(proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass

    def _wait_for_child_exit(self, proc: subprocess.Popen, timeout: float = STOP_TIMEOUT_SECONDS) -> int:
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            return proc.wait()

    def _start_tray(self) -> None:
        if not self.use_tray or self._tray is not None:
            return
        menu = [
            MenuItem(id=1, label='Open Card Scanner', callback=self.open_browser),
            MenuItem(id=2, separator=True),
            MenuItem(id=3, label='Restart', callback=self.request_restart),
            MenuItem(id=4, label='Quit', callback=self.request_quit),
        ]
        tray = TrayIcon(
            app_id='mtg-scanner',
            title='Card Scanner',
            tooltip='Running — click to open',
            icon_name='mtg-scanner',
            icon_theme_path=self.icon_dir,
            on_activate=self.open_browser,
            menu_items=menu,
        )
        if tray.start():
            self._tray = tray

    def _stop_tray(self) -> None:
        if self._tray is not None:
            self._tray.stop()
            self._tray = None

    def run(self) -> int:
        # If another instance already holds app.lock, let app.py open the running browser tab and exit
        # cleanly without flashing a second tray icon.
        already_running = is_lock_held(self.user_dir)
        if already_running:
            proc = subprocess.Popen(self._command())
            return proc.wait()

        # Forward SIGINT / SIGTERM to the supervised server cleanly
        def handle_signal(signum, frame):
            self.request_quit()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        extra_args: list[str] = []
        exit_code = 0
        try:
            while True:
                proc = subprocess.Popen(self._command(extra_args))
                with self._lock:
                    self._process = proc
                    self._restart_requested = False

                # Wait briefly before showing the tray so immediate failures or port conflicts never flash an icon
                _, host, port = compute_local_url(self.app_dir, self.user_dir)
                deadline = time.monotonic() + 3.0
                while proc.poll() is None and not self._quit_requested and not self._restart_requested:
                    if port_answers(host, port) or time.monotonic() >= deadline:
                        break
                    self._wake.wait(timeout=0.1)
                    self._wake.clear()

                if proc.poll() is None and not self._quit_requested:
                    self._start_tray()
                    if self._tray is not None:
                        self._tray.set_tooltip('Running — click to open')

                # Wait until the child exits (or quit/restart kills it cleanly)
                while proc.poll() is None:
                    if self._quit_requested or self._restart_requested:
                        self._wait_for_child_exit(proc)
                        break
                    self._wake.wait(timeout=0.25)
                    self._wake.clear()

                exit_code = proc.returncode if proc.returncode is not None else 0
                with self._lock:
                    self._process = None
                    should_restart = self._restart_requested and not self._quit_requested

                if should_restart:
                    extra_args = ['--no-browser']
                    continue
                break
        finally:
            with self._lock:
                proc = self._process
            if proc is not None and proc.poll() is None:
                self._terminate_child(proc)
                self._wait_for_child_exit(proc)
            self._stop_tray()

        return exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    app_dir, icon_dir = find_app_paths()

    # Pass-through flags (--quit, --support-bundle, -h, --help) bypass the tray completely
    if any(flag in PASSTHROUGH_FLAGS for flag in args):
        os.execv(sys.executable, [sys.executable, '-B', str(app_dir / 'app.py'), '--app', *args])

    use_tray = True
    passthrough: list[str] = []
    for arg in args:
        if arg == '--no-tray':
            use_tray = False
        else:
            passthrough.append(arg)

    return Supervisor(app_dir, icon_dir, passthrough, use_tray=use_tray).run()


if __name__ == '__main__':
    sys.exit(main())
