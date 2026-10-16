# ============================================================================
# FILE: server/program.py
# The server as a program on somebody's computer: started as a desktop program (--app: the
# AppImage), told to quit or restart from Settings, who may reach it over the network, and
# the camera station that runs beside it when this machine has a camera.
#
# What the desktop program is and the server on a server differ in this file and in the
# launchers - nothing else (see CLAUDE.md: the standalone is derived from this repository).
# ============================================================================
import argparse
import os
import signal
import subprocess
import sys
import threading
import time

from flask import jsonify, request

from card_scanner.config import Config
from card_scanner.paths import TOP
from card_scanner.server.logs import logger
from card_scanner.server.state import app, services

LOCAL_HOSTS = ('127.0.0.1', 'localhost', '::1')
LOOPBACK = ('127.0.0.1', '::1', '::ffff:127.0.0.1')
STATION_STOP_SECONDS = 10  # a station that has not ended by then is killed


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description='Card Scanner server')
    parser.add_argument('--app', action='store_true',
                        help='run as a desktop program: open the page in the browser, offer Quit in Settings, '
                             'start this computer\'s camera station, listen on this computer only')
    parser.add_argument('--no-browser', action='store_true', help="with --app: don't open the browser")
    parser.add_argument('--no-station', action='store_true', help="with --app: don't start a camera station")
    parser.add_argument('--quit', action='store_true', help='stop the program started with --app')
    return parser.parse_args(argv)


# ----------------------------------------------------------------------------
# Who may reach it
# ----------------------------------------------------------------------------

def network_access():
    """
    Whether other devices on the network may open the pages (there is no login): the switch
    in Settings, else flask.host - 'auto' means yes, except as a desktop program (--app)
    """
    saved = services.app_settings.get('network_access') if services.app_settings else None
    if saved is not None:
        return bool(saved)
    if Config.HOST == 'auto':
        return not services.app_mode
    return Config.HOST not in LOCAL_HOSTS


def debugger_allowed(host):
    """
    Whether Debug mode may also turn on Werkzeug's interactive debugger: only when the server
    listens on this computer alone (loopback), or when the person asked for it on the command line
    of this very process - the environment variable SCANNER_LAN_DEBUGGER=1. On a server that other
    devices reach, an error page with a Python console would be one step from running code for
    anyone on the network (the PIN is written to the log), and the switch that turns it on is
    reachable from the network too. The request log (Debug mode's other half) is always kept.
    """
    return host in LOCAL_HOSTS or os.environ.get('SCANNER_LAN_DEBUGGER') == '1'


def listen_host():
    """The address the server listens on (taken when it starts)"""
    if not network_access():
        return '127.0.0.1'
    # An address of one's own in config.yaml stays; 'auto' and local ones open up
    return '0.0.0.0' if Config.HOST == 'auto' or Config.HOST in LOCAL_HOSTS else Config.HOST


@app.context_processor
def program_state():
    """What the settings drawer of every page needs: the program section and its switches"""
    return {'app_mode': services.app_mode, 'shown': Config.shown,
            'network_access': network_access(), 'network_access_running': services.network_access_running}


@app.route('/api/network_access', methods=['POST'])
def set_network_access():
    """Remember whether other devices may open the pages (JSON: 'enabled'); the next start takes it"""
    enabled = bool((request.get_json(silent=True) or {}).get('enabled'))
    services.app_settings.set('network_access', enabled)
    logger.info(f"Network access: {enabled} (running: {services.network_access_running})")
    return jsonify({'success': True, 'enabled': enabled, 'running': bool(services.network_access_running)})


# ----------------------------------------------------------------------------
# Quit and restart
# ----------------------------------------------------------------------------

_stop_lock = threading.Lock()
_stop_scheduled = False  # a stop is on its way: another request must not schedule a second one
_shutting_down = threading.Event()  # the first SIGTERM was taken: main is cleaning up


def stop_on_signal(signum, frame):
    # Ends socketio.run like Ctrl+C does, so main closes the databases and ends the station.
    # Only the first: a second one (two tabs asking, a service manager's repeat) would raise
    # inside the cleanup and skip the rest of it - and the restart
    if _shutting_down.is_set():
        return
    _shutting_down.set()
    raise KeyboardInterrupt


def _stop_soon():
    """Stop the program shortly (after the answer is sent) - once, however often it is asked"""
    global _stop_scheduled
    with _stop_lock:
        if _stop_scheduled:
            return
        _stop_scheduled = True
    threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()


@app.route('/api/restart', methods=['POST'])
def restart_program():
    """Stop and start the program again - what the settings that apply "after a restart" wait for"""
    services.restart_requested = True
    logger.info("Restart requested from the web interface")
    _stop_soon()
    return jsonify({'success': True})


@app.route('/api/quit', methods=['POST'])
def quit_program():
    """Stop the program (desktop program only, and only from the computer it runs on: a
    server is stopped with its service or container)"""
    if not services.app_mode:
        return jsonify({'error': 'Only available when started as a desktop program (--app)'}), 403
    if request.remote_addr not in LOOPBACK:
        return jsonify({'error': 'Quit works from the computer the program runs on'}), 403
    logger.info("Quit requested from the web interface")
    if not _shutting_down.is_set():
        services.restart_requested = False  # a quit after a restart request, before it happened, wins
    _stop_soon()
    return jsonify({'success': True})


def exec_again_if_requested():
    """After the program has shut down: the same command in this very process (a service or the
    AppImage keep their program; the lock on the data folder and the port are released by the
    exec). The page that asked reloads by itself, so no second browser tab"""
    if not services.restart_requested:
        return
    command = [sys.executable] + sys.orig_argv[1:]
    if services.app_mode and '--no-browser' not in command:
        command.append('--no-browser')
    logger.info("Restarting")
    print("Restarting...\n")
    sys.stdout.flush()
    os.execv(sys.executable, command)


# ----------------------------------------------------------------------------
# The camera station beside the server
# ----------------------------------------------------------------------------

def connect_host():
    """
    The address this computer reaches its own server on: loopback for a server that listens on
    every address, the address itself for one that listens on an address of its own
    (flask.host: 192.168.1.5 answers there and not on loopback).
    """
    host = listen_host()
    if host in ('0.0.0.0', '', '::', '127.0.0.1', 'localhost', '::1'):
        return 'localhost'
    return host


def local_url(port):
    host = connect_host()
    return f"http://[{host}]:{port}" if ':' in host else f"http://{host}:{port}"


def candidate_urls(port):
    """
    Where a running program may be reached from this computer, before the saved settings are
    read (--quit, a second start): loopback, and the address flask.host names. What is saved
    ("Reachable from other devices") may have moved it to the other of the two.
    """
    hosts = ['localhost']
    if Config.HOST not in ('auto', '0.0.0.0', '', '::') and Config.HOST not in LOCAL_HOSTS:
        hosts.append(Config.HOST)
    return [f"http://[{host}]:{port}" if ':' in host else f"http://{host}:{port}" for host in hosts]


def station_command(port):
    return [sys.executable, str(TOP / 'station.py'), '--server', local_url(port)]


def start_station(port):
    """
    This computer's camera is read by a camera station, a second program that sends its cards
    here; started with the server and ended with it. It keeps trying until the server
    answers, so it does not matter that it starts first. Returns the process, or None when
    there is nothing to start (camera.type: remote - this machine has no camera).
    """
    if str(Config.CAMERA_TYPE).lower() == 'remote':
        return None
    src = str(TOP / 'src')
    env = {**os.environ, 'PYTHONPATH': src + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')}
    if not network_access():
        env.setdefault('SCANNER_SETUP_HOST', '127.0.0.1')  # the station's setup page has no login either
    try:
        process = subprocess.Popen(station_command(port), cwd=str(TOP), env=env)
    except OSError as e:
        logger.warning(f"The camera station could not be started: {e}")
        return None
    services.station_process = process
    logger.info(f"Camera station started (pid {process.pid})")
    return process


def stop_station():
    """
    Ask the station to stop (it lets go of the camera), and give it STATION_STOP_SECONDS; then
    kill it - the server must never hang here, and nothing is lost: captures that are not on
    the server yet are on disk and sent at the next start.
    """
    process, services.station_process = services.station_process, None
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    deadline = time.time() + STATION_STOP_SECONDS
    while process.poll() is None and time.time() < deadline:
        time.sleep(0.2)
    if process.poll() is None:
        logger.warning("The camera station did not stop by itself - ending it")
        process.kill()
        process.wait()
