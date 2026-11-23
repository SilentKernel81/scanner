# ============================================================================
# FILE: diagnostics.py
# What a program says about itself when something goes wrong, for the server and for a station:
#
#   build_info        which build this is (the project has no version numbers: a build is a commit)
#   install_crash_capture   what would otherwise die with the process goes to data/logs/crash-<program>.log
#   bound_log         keeps a file that no logging handler rotates from growing for ever
#
# Only the standard library and paths.py on purpose: this has to work when the rest does not (a broken
# config.yaml, a library that fails to import), and a station's image holds only the shared modules.
# ============================================================================
import faulthandler
import functools
import os
import subprocess
import sys
import threading
import time
import traceback

from card_scanner.paths import CODE, TOP, USER_DIR

STAMP_FILE = CODE / 'build_stamp.txt'     # filled by `git archive` (.gitattributes: export-subst) or by the AppImage's build
LOG_DIR = USER_DIR / 'data' / 'logs'
CRASH_LOG_BYTES = 2 * 1024 * 1024         # a crash log is short; one that is not is the same crash, over and over

_captured = {}                            # program -> its open crash file: kept open for faulthandler, which writes to it while crashing


def _parse_stamp(text):
    """{'commit', 'tree', 'date', 'modified'} from '<commit> <tree> <date> [modified]', or None for an empty or unfilled stamp"""
    parts = (text or '').split()
    if len(parts) < 3 or '$Format' in text or len(parts[0]) < 7:
        return None
    return {'commit': parts[0], 'tree': parts[1], 'date': parts[2], 'modified': 'modified' in parts[3:]}


def _from_git():
    """The stamp of a checkout, from git itself; None without git or outside a repository"""
    if not (TOP / '.git').exists():
        return None
    try:
        command = ['git', '-C', str(TOP)]
        head = subprocess.run(command + ['log', '-1', '--format=%H %T %cI'], capture_output=True, text=True, timeout=3)
        if head.returncode:
            return None
        status = subprocess.run(command + ['status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, timeout=5)
        return _parse_stamp(head.stdout + (' modified' if status.stdout.strip() else ''))
    except (OSError, subprocess.SubprocessError):
        return None


@functools.lru_cache(maxsize=1)
def build_info():
    """
    Which build this is: {'commit', 'tree', 'date', 'modified', 'source'}. The tree says more than the commit between the two repositories: the standalone's
    HEAD is a merge commit of its own, while its tree is the same as the server's. A build whose stamp was never filled (a Docker image built from a clone) says
    'unknown' rather than guessing.
    """
    try:
        stamp = _parse_stamp(STAMP_FILE.read_text())
    except OSError:
        stamp = None
    if stamp:
        return {**stamp, 'source': 'stamp'}
    stamp = _from_git()
    if stamp:
        return {**stamp, 'source': 'git'}
    return {'commit': 'unknown', 'tree': 'unknown', 'date': 'unknown', 'modified': False, 'source': 'none'}


def describe_build():
    """One line for the log: 'd8920bf (2026-11-23T15:47:00-06:00)', with ' + local changes' when the checkout differs from its commit"""
    info = build_info()
    if info['commit'] == 'unknown':
        return 'unknown build'
    return f"{info['commit'][:7]} ({info['date']})" + (' + local changes' if info['modified'] else '')


def bound_log(path, limit):
    """
    A file written by something other than a logging handler (a subprocess's stderr): above `limit` bytes it becomes <name>.1, replacing the one before.
    Call it before the file is opened. Never raises: a log is never a reason not to start.
    """
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, f'{path}.1')
    except OSError:
        pass


def install_crash_capture(program):
    """
    From now on, what would die with this process is written to data/logs/crash-<program>.log: a fatal signal in native code (OpenCV, ONNX: faulthandler, all
    threads), an exception nobody caught, in the main thread or in any other. One file per program, because the server and a station share a data folder in the
    desktop program. Called by each program's main(), never on import - the tests import both. Never raises.
    """
    if program in _captured:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f'crash-{program}.log'
        bound_log(path, CRASH_LOG_BYTES)
        crash_file = open(path, 'a', buffering=1, encoding='utf-8', errors='replace')
    except OSError:
        return
    _captured[program] = crash_file
    write_lock = threading.Lock()

    def note(where, kind, value, trace):
        # Inside a hook: whatever happens here must not hide the original error
        try:
            text = ''.join(traceback.format_exception(kind, value, trace))
            with write_lock:
                crash_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} uncaught in {where}\n{text}\n")
        except Exception:
            pass

    try:
        crash_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {program} started, pid {os.getpid()}, build {describe_build()}\n")
        faulthandler.enable(file=crash_file, all_threads=True)
    except (OSError, ValueError, RuntimeError):
        pass

    previous_main, previous_thread = sys.excepthook, threading.excepthook

    def main_hook(kind, value, trace):
        if not issubclass(kind, KeyboardInterrupt):         # Ctrl+C is how a program is stopped, not a crash
            note('the main thread', kind, value, trace)
        previous_main(kind, value, trace)

    def thread_hook(args):
        if args.exc_type is not SystemExit:                 # how a thread ends itself
            name = args.thread.name if args.thread is not None else 'a thread'
            note(f'thread {name}', args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread(args)

    sys.excepthook, threading.excepthook = main_hook, thread_hook
