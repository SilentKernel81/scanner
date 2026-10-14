#!/usr/bin/env python3
"""
Measures how fast a camera station runs on this machine, with its real camera - to compare
one way of running it with another (in the Docker container or straight from Python, one Pi
with another, before and after a change):

    python3 scripts/benchmark_station.py                 # 20 s per phase
    python3 scripts/benchmark_station.py --seconds 30 --label "in the container"

The station must be stopped first: a camera can be open in one program only.

In the container the script is not part of the image, so its folder is handed in:

    docker run --rm --privileged -v /dev:/dev -v /run/udev:/run/udev:ro \\
        -v <the station's data volume>:/app/data -v "$PWD/scripts":/app/scripts:ro \\
        --entrypoint python3 mtg-scanner-station-pi scripts/benchmark_station.py

What it does:
  1. Starts the scanner exactly as the station would - the camera and the crop chosen on the
     setup page (data/station.json), or config.yaml - but without a server.
  2. Phase "scanning": lets it run and counts the frames it gets through.
  3. Phase "scanning + live view": the same while the live picture is encoded, as when the
     camera's page is open in a browser.
  4. Phase "detector alone": takes frames it kept from the camera and runs only the outline
     detector on them, again and again. No camera and no waiting are involved here, so this
     number is the cleanest comparison of two environments.

For each phase it prints frames a second, how much of the processor this program used
(100% = one core busy all the time) and the temperature. Put a card in the box before
starting: with a card in view the station does more work per frame than over an empty box.

Nothing is sent anywhere and no card is captured.
"""
import argparse
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path

# The station's modules live one folder up from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))  # the code is in src/
os.chdir(Path(__file__).resolve().parent.parent)

import cv2  # noqa: E402
import numpy  # noqa: E402

from card_scanner.station import object_detector  # noqa: E402
from card_scanner.station import station_setup  # noqa: E402
from card_scanner.config import Config  # noqa: E402


class NoServer(dict):
    """Stands in for the camera settings a server would send (CardScanner asks get / set)"""

    def set(self, key, value):
        self[key] = value


def temperature():
    """The processor's temperature in °C, or None where the machine does not say"""
    return station_setup.machine_temperature()


def processor_seconds():
    """Processor time this program has used so far, all its threads together (user + system)"""
    used = os.times()
    return used.user + used.system


def measure(scanner, seconds, label, during=None):
    """
    Let the scanner run for `seconds` and report how it went. `during` is something to keep
    doing meanwhile, in a thread of its own (the live view's encoding).
    """
    stop = threading.Event()
    if during:
        threading.Thread(target=during, args=(stop,), daemon=True).start()
    time.sleep(2)  # let the frame rate settle after the change
    frames, clock, used = scanner.frame_id, time.time(), processor_seconds()
    seen = 0     # how many times we looked ...
    with_card = 0  # ... and how often a card was in view
    kept = []    # some of the frames, for the "detector alone" phase
    while time.time() - clock < seconds:
        time.sleep(0.25)
        seen += 1
        with_card += bool(scanner.card_detected)
        small = scanner.current_small  # the Pi camera's small frame, where it sends one
        frame = small if small is not None else scanner.current_frame
        if frame is not None and len(kept) < 40:
            kept.append(frame.copy())
    elapsed = time.time() - clock
    stop.set()
    rate = (scanner.frame_id - frames) / elapsed
    load = (processor_seconds() - used) / elapsed * 100
    print(f"  {label:<24} {rate:5.1f} frames a second   processor {load:4.0f}%   "
          f"card in view {100 * with_card / max(seen, 1):3.0f}% of the time   {temperature()} °C")
    return rate, load, kept


def live_view(scanner):
    """What the station does while its page is watched: encode the picture ~12 times a second"""
    def run(stop):
        while not stop.is_set():
            scanner.get_stream_jpeg()
            time.sleep(0.08)  # station_client.PREVIEW_INTERVAL
    return run


def detector_alone(frames, rounds=5):
    """
    The outline detector on the kept frames, without a camera: milliseconds per frame, the
    median over all runs. "Following" hands it the previous frame's outline, as the scanner
    does when a card lies still - a different path through the detector than starting afresh.
    """
    alone, following = [], []
    for _ in range(rounds):
        previous = None
        for frame in frames:
            start = time.perf_counter()
            corners, _ = object_detector.find_card_outline(frame)
            alone.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            object_detector.find_card_outline(frame, previous=previous)
            following.append((time.perf_counter() - start) * 1000)
            previous = corners if corners is not None else previous
    return statistics.median(alone), statistics.median(following)


def main():
    parser = argparse.ArgumentParser(description="How fast a camera station runs here")
    parser.add_argument('--seconds', type=float, default=20, help="length of each phase (default 20)")
    parser.add_argument('--label', default='', help="a name for this run, printed with the result")
    args = parser.parse_args()

    # The same camera and crop the station would use (see station_setup.resolve)
    values, fixed = station_setup.resolve({}, station_setup.load(), platform.node())
    station_setup.apply_camera(values, fixed)
    Config.create_directories()

    where = "in a container" if Path('/.dockerenv').exists() else "straight from Python"
    print(f"Station benchmark{' - ' + args.label if args.label else ''}")
    print(f"  {platform.node()}, {where}; Python {platform.python_version()}, OpenCV {cv2.__version__}, "
          f"NumPy {numpy.__version__}; {os.cpu_count()} cores; {temperature()} °C at the start")

    from card_scanner.station.scanner import CardScanner  # opens the camera
    scanner = CardScanner(log_callback=lambda message, level="info": None,
                          settings=NoServer(camera_rotation=0))
    try:
        deadline = time.time() + 15
        while scanner.current_frame is None and time.time() < deadline:
            time.sleep(0.2)
        if scanner.current_frame is None:
            sys.exit(f"  No picture from the camera: {scanner.camera_error or 'nothing arrived in 15 s'}")
        frame = scanner.current_frame
        print(f"  camera: {scanner.camera_type}, frames of {frame.shape[1]}x{frame.shape[0]}"
              + (f", small frames of {scanner.current_small.shape[1]}x{scanner.current_small.shape[0]}"
                 if scanner.current_small is not None else ""))

        _, _, kept = measure(scanner, args.seconds, "scanning")
        measure(scanner, args.seconds, "scanning + live view", during=live_view(scanner))
    finally:
        scanner.cleanup()  # release the camera, also when something above failed

    if kept:
        cv2.setNumThreads(2)  # as scanner.py sets it
        alone, following = detector_alone(kept)
        found = sum(object_detector.find_card_outline(frame)[0] is not None for frame in kept)
        print(f"  {'detector alone':<24} {alone:5.1f} ms a frame afresh, {following:5.1f} ms following the previous outline"
              f"   ({len(kept)} frames, a card found in {found})")


if __name__ == '__main__':
    main()
