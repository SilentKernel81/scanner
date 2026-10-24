"""
The card dropper's controller (hardware/dropper/firmware/main.py): how a pulse from the station becomes a feed, on a
simulated Raspberry Pi Pico. No MicroPython and no Pico: a stand-in for its
`machine` module (pins, PWM, a timer) and a virtual clock - `time.sleep_ms` moves the clock and calls the timer's callback
once per millisecond, as the Pico's scheduler does while the firmware sleeps.

What it pins (a Codex review of 2026-10-23): the station pulses as soon as the card just released has been captured, which
can be before the mechanism is back at rest - a pulse then must be carried out once, right after the feed, never lost (the
feeding stopped for good) and never doubled (two cards on the pile).

    venv/bin/python -m unittest discover tests -p test_dropper_firmware.py
"""
import importlib.util
import runpy
import sys
import types
import unittest
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parent.parent / 'hardware' / 'dropper' / 'firmware'


class EndOfRun(Exception):
    """The simulated time is up: stops the firmware's endless main loop."""


class Clock:
    """Virtual milliseconds. Every millisecond slept, each timer's callback runs once."""

    def __init__(self, limit):
        self.now = 0
        self.limit = limit
        self.timers = []
        self.led_on = []          # when the board's LED went on: feed_one lights it for each card

    def sleep_ms(self, ms):
        for _ in range(int(ms)):
            self.now += 1
            for callback in self.timers:
                callback(None)
            if self.now >= self.limit:
                raise EndOfRun()

    def ticks_ms(self):
        return self.now

    @staticmethod
    def ticks_diff(a, b):
        return a - b


def fake_machine(clock, line):
    """A stand-in for MicroPython's `machine`: `line(now)` says whether the station's pin is high."""
    machine = types.ModuleType('machine')

    class Pin:
        IN, OUT, PULL_UP, PULL_DOWN = 0, 1, 2, 3

        def __init__(self, ident, mode=None, pull=None):
            self.ident = ident

        def value(self):
            return 1 if line(clock.now) else 0

        def on(self):
            if self.ident == 'LED':
                clock.led_on.append(clock.now)

        def off(self):
            pass

    class PWM:
        def __init__(self, pin):
            self.duty = 0

        def freq(self, hz):
            pass

        def duty_ns(self, ns):
            self.duty = ns

    class Timer:
        PERIODIC = 1

        def __init__(self, mode=None, freq=None, callback=None):
            assert freq == 1000, 'the trigger is sampled every millisecond'
            clock.timers.append(callback)

    machine.Pin, machine.PWM, machine.Timer = Pin, PWM, Timer
    machine.disable_irq = lambda: 0
    machine.enable_irq = lambda state: None
    return machine


def load(name, clock, line):
    """The firmware file as a module, its `machine` and `time` the stand-ins (its main loop is not started)."""
    sys.modules['machine'] = fake_machine(clock, line)
    try:
        spec = importlib.util.spec_from_file_location(f'firmware_{name}', FIRMWARE / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        del sys.modules['machine']
    module.time = clock                  # time.sleep_ms / ticks_ms: the virtual clock
    return module


def pulses(*spans):
    """The station's line: high during each (start, length) in ms."""
    return lambda now: any(start <= now < start + length for start, length in spans)


class FeedRecorder:
    """Runs a firmware's main() on the virtual clock and notes when each feed began and ended."""

    def __init__(self, name, line, limit=12000):
        self.clock = Clock(limit)
        self.module = load(name, self.clock, line)
        original = self.module.feed_one
        self.feeds = []

        def recorded():
            began = self.clock.now
            original()
            self.feeds.append((began, self.clock.now))

        self.module.feed_one = recorded
        try:
            self.module.main()
        except EndOfRun:
            pass

    @property
    def starts(self):
        return [began for began, _ in self.feeds]


def boot_as_the_pico_does(line, limit):
    """Runs the firmware file as `__main__`, the way the Pico runs main.py at power-up - the real entry, with its pause -
    on the virtual clock. Returns when each feed began (the LED going on)."""
    clock = Clock(limit)
    saved = sys.modules.get('time')
    sys.modules['machine'] = fake_machine(clock, line)
    sys.modules['time'] = clock          # the firmware's `import time`: the virtual clock (nothing else runs meanwhile)
    try:
        runpy.run_path(str(FIRMWARE / 'main.py'), run_name='__main__')
    except EndOfRun:
        pass
    finally:
        del sys.modules['machine']
        sys.modules['time'] = saved
    return clock.led_on


class Firmware(unittest.TestCase):
    name = 'main.py'

    def feed_length(self):
        """How long one feed takes on this firmware, measured on the simulation."""
        return FeedRecorder(self.name, pulses((1000, 150))).feeds[0][1] - 1000 - 8

    def test_one_pulse_one_feed(self):
        run = FeedRecorder(self.name, pulses((1000, 150)))
        self.assertEqual(len(run.feeds), 1)
        self.assertLess(run.starts[0] - 1000, 20, 'the feed begins as the pulse is recognised (8 ms)')

    def test_a_pulse_during_the_return_is_carried_out_once_after_it(self):
        # The finding: the station's next pulse comes while the mechanism is still on its way back
        length = self.feed_length()
        first = 1000
        late = first + 8 + length - 150          # 150 ms before this feed ends: the return (and the limp pause)
        run = FeedRecorder(self.name, pulses((first, 150), (late, 150)), limit=first + 3 * length + 2000)
        self.assertEqual(len(run.feeds), 2, 'lost or doubled')
        self.assertLessEqual(run.starts[1] - run.feeds[0][1], 2, 'carried out as soon as the first feed ended')
        self.assertEqual(run.module.trigger.dropped, 0)

    def test_a_pulse_early_in_a_feed_waits_for_it(self):
        length = self.feed_length()
        run = FeedRecorder(self.name, pulses((1000, 150), (1300, 150)), limit=1000 + 3 * length + 2000)
        self.assertEqual(len(run.feeds), 2)
        self.assertGreaterEqual(run.starts[1], run.feeds[0][1], 'never two feeds at once')

    def test_a_pulse_still_on_when_the_feed_ends_counts_once(self):
        # 2000 ms is the longest pulse the station sends; it ends near a feed's end (~2.4 s)
        run = FeedRecorder(self.name, pulses((1000, 2000)), limit=9000)
        self.assertEqual(len(run.feeds), 1)

    def test_noise_shorter_than_8_ms_releases_nothing(self):
        run = FeedRecorder(self.name, pulses((1000, 5), (1500, 7), (2000, 1)), limit=4000)
        self.assertEqual(run.feeds, [])

    def test_the_station_s_shortest_pulse_counts(self):
        run = FeedRecorder(self.name, pulses((1000, 10)), limit=4000)
        self.assertEqual(len(run.feeds), 1)

    def test_a_pulse_during_start_up_is_carried_out_after_it(self):
        run = FeedRecorder(self.name, pulses((100, 150)), limit=5000)
        self.assertEqual(len(run.feeds), 1)

    def test_a_pulse_during_the_boot_pause_is_not_lost(self):
        # The finding of 2026-10-24: the pin was watched only after the 1.5 s pause, so a Pico that reset while the station
        # ran lost a pulse in it - and the station, which counted that card as released, waited for it for good
        feeds = boot_as_the_pico_does(pulses((300, 150)), limit=6000)
        self.assertEqual(len(feeds), 1, 'the pulse in the pause was lost')
        self.assertGreaterEqual(feeds[0], 1500 + 600, 'fed only after the pause and the servos at rest')

    def test_the_real_entry_feeds_a_pulse_after_the_pause_too(self):
        feeds = boot_as_the_pico_does(pulses((3000, 150)), limit=7000)
        self.assertEqual(len(feeds), 1)
        self.assertLess(feeds[0] - 3000, 20)

    def test_a_pulse_already_on_at_power_up_is_not_fed(self):
        # The finding of 2026-10-24 (second review): the Pico resets in the middle of a feed - a brown-out as the servos
        # start - while the station's pulse (up to 2 s) is still on. The card has kept the travel it got before the
        # reset (one-way bearings); a whole new feed on top of it would turn axle 3 under the next card
        feeds = boot_as_the_pico_does(pulses((0, 2000)), limit=6000)
        self.assertEqual(feeds, [], 'an interrupted feed was replayed')

    def test_after_a_pulse_on_at_power_up_the_next_one_counts(self):
        # Armed once the line was seen inactive: the station's next request is fed as usual
        feeds = boot_as_the_pico_does(pulses((0, 800), (3000, 150)), limit=7000)
        self.assertEqual(len(feeds), 1)
        self.assertLess(feeds[0] - 3000, 20)

    def test_a_pulse_beginning_in_the_pause_still_counts_after_a_quiet_start(self):
        # The line is inactive at the first sample, so the trigger is armed at once: a pulse from 1 ms on is a request
        feeds = boot_as_the_pico_does(pulses((5, 150)), limit=6000)
        self.assertEqual(len(feeds), 1)

    def test_more_than_one_waiting_request_is_not_carried_out(self):
        # Cannot come from the station (it asks only after the previous card was captured); if it did,
        # one card too few (the feeding stops) is the safe side, not two cards on the pile
        run = FeedRecorder(self.name, pulses((1000, 150), (1300, 50), (1500, 50)), limit=9000)
        self.assertEqual(len(run.feeds), 2)
        self.assertEqual(run.module.trigger.dropped, 1)

    def test_active_low(self):
        clock = Clock(10)
        module = load(self.name, clock, lambda now: False)
        levels = iter([1] + [0] * 8)     # high (inactive) at the first sample - armed - then low (active)
        trigger = module.Trigger(type('P', (), {'value': lambda self: next(levels)})(), True, 8)
        for _ in range(9):
            trigger.sample()
        self.assertTrue(trigger.take())
        self.assertFalse(trigger.take(), 'taken once')


if __name__ == '__main__':
    unittest.main()
