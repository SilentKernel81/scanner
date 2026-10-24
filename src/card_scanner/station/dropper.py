"""
The card dropper: a mechanism under the station's control that releases the next card onto the pile, so nobody has to drop
cards by hand (docs/plans/PRODUCT_PLAN.md, phase 5 - the owner's idea of 2026-10-22; the mechanism itself is not designed yet).

This module only PULSES ONE GPIO PIN for a moment. What the pin drives - a servo's driver, a relay, a solenoid's transistor, a
stepper driver's step input - is the mechanism's business, not this module's: one pulse means "release one card".

WHEN a pulse is sent is the station's decision (station_client.py: Station.release_next_card and its callers), never this
module's: after the capture beep (the picture taken and any focus probe it started done), with auto-add off only once the card
was added or skipped on the page, and the first card when auto-scanning is switched on. One pulse per card and never more: a
card that is not captured (jammed, the magazine empty, the camera lost) releases nothing, so the stack can never run on by itself.

The pin is reached through the kernel's GPIO character device with libgpiod 2 (the `gpiod` package, LGPL-2.1). It is imported
only when a pulse is due: a station without the package, or a PC with no GPIO at all, runs exactly as before, and the dropper
says why it cannot work (`Dropper.error`, shown on the Settings page). On a Raspberry Pi 4 and 5 the 40-pin header is
/dev/gpiochip0 and a pin's GPIO number (the BCM number, "GPIO17") is its line offset there - checked on 2026-10-22 on both
(pinctrl-bcm2711, 58 lines; pinctrl-rp1, 54 lines; /dev/gpiochip4 is the same chip under its older name). The chip stays a
setting in case a later kernel numbers them otherwise.

Its settings live on the server with the camera's others (remote_scanner.CAMERA_SETTINGS 'dropper'), like every scanning
setting; `checked` below is the one place their rules are written, used by the server before saving and by the station
before using them.
"""
import logging
import threading
import time

logger = logging.getLogger('station')

# The dropper's settings and what they are until someone changes them. Off: a station without a dropper never pulses a pin
DEFAULTS = {
    'enabled': False,
    'chip': '/dev/gpiochip0',   # the GPIO chip's device (the Pi's 40-pin header on a Pi 4 and 5)
    'pin': None,                # the GPIO number (BCM) of the pin, as printed on Pi pinout charts: GPIO17 -> 17
    'pulse_ms': 150,            # how long the pin stays active for one card
    'active_low': False,        # True: "active" is the pin LOW (a relay board that switches on a low input)
}
PIN_RANGE = (0, 63)             # the header's GPIOs are 0-27; the chips have up to 58 lines - beyond 63 is a typing error
PULSE_RANGE = (10, 2000)        # milliseconds: shorter is lost in the driver's own reaction time, longer is a mechanism stuck open


def checked(config):
    """
    The dropper's settings with every value checked, the missing ones from DEFAULTS - or ValueError naming the wrong one,
    before anything is used or saved. None (never set) is the defaults: off.
    """
    if config is None:
        return dict(DEFAULTS)
    if not isinstance(config, dict):
        raise ValueError('The card dropper\'s settings are not a set of values')
    unknown = sorted(set(config) - set(DEFAULTS))
    if unknown:
        raise ValueError(f'Unknown card dropper setting: {", ".join(unknown)}')
    values = dict(DEFAULTS, **config)
    for flag in ('enabled', 'active_low'):
        if not isinstance(values[flag], bool):
            raise ValueError(f'The card dropper\'s {flag} is on or off (true / false)')
    chip = values['chip']
    # A device of the GPIO chips and nothing else: the setting is typed on a web page and opened on the station
    if not isinstance(chip, str) or not chip.startswith('/dev/gpiochip') or not chip[len('/dev/gpiochip'):].isdigit():
        raise ValueError('The GPIO chip is a device like /dev/gpiochip0')
    pin = values['pin']
    # isinstance(True, int) is true in Python: a switch's value sent where a number belongs is refused, not taken as GPIO 1
    if pin is not None and (isinstance(pin, bool) or not isinstance(pin, int) or not PIN_RANGE[0] <= pin <= PIN_RANGE[1]):
        raise ValueError(f'The GPIO pin is a number from {PIN_RANGE[0]} to {PIN_RANGE[1]} (GPIO17 is 17)')
    pulse = values['pulse_ms']
    if isinstance(pulse, bool) or not isinstance(pulse, int) or not PULSE_RANGE[0] <= pulse <= PULSE_RANGE[1]:
        raise ValueError(f'The pulse lasts {PULSE_RANGE[0]} to {PULSE_RANGE[1]} ms')
    if values['enabled'] and pin is None:
        raise ValueError('Choose the GPIO pin before switching the card dropper on')
    return values


class GpiodPin:
    """
    One output line through libgpiod 2 (`gpiod`), requested when the first pulse is due and kept until the settings change or
    the station stops. Requested INACTIVE: asking for the line must never release a card by itself.
    """

    def __init__(self, chip, pin, active_low):
        try:
            import gpiod                                   # only here: a station that never pulses never needs it
            from gpiod.line import Direction, Value
        except ImportError:
            raise RuntimeError('the GPIO library (gpiod) is not installed on this station') from None
        self._active, self._inactive = Value.ACTIVE, Value.INACTIVE
        self.pin = pin
        settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=active_low, output_value=Value.INACTIVE)
        try:
            self._request = gpiod.request_lines(chip, consumer='card-scanner-dropper', config={pin: settings})
        except FileNotFoundError:
            raise RuntimeError(f'{chip} does not exist on this machine (no GPIO, or the container was not given it)') from None
        except PermissionError:
            raise RuntimeError(f'no permission to use {chip} (in Docker: deploy/docker-compose.client-gpio.yml)') from None
        except OSError as e:     # the line is used by something else, or the chip has no such line
            raise RuntimeError(f'GPIO{pin} on {chip} cannot be used: {e.strerror or e}') from None

    def set(self, active):
        self._request.set_value(self.pin, self._active if active else self._inactive)

    def close(self):
        try:
            self._request.release()
        except Exception:
            pass        # the line goes when the process goes anyway


class Dropper:
    """
    The station's card dropper: its settings (configure) and one pulse at a time (pulse). Thread-safe: the capture's thread,
    the server's commands and the review's answer may all ask for a pulse; a second one while the first is still on its way is
    refused, never queued - one card per pulse.
    """

    def __init__(self, open_pin=GpiodPin, sleep=time.sleep):
        self.config = dict(DEFAULTS)
        self._open_pin = open_pin          # tests hand in a stand-in for the pin
        self._sleep = sleep
        self._pin = None
        self._lock = threading.Lock()      # held for the whole pulse
        self.pulses = 0                    # cards released since the station started
        self.error = None                  # why the last pulse could not be sent (None: it was, or none was asked for)
        self.last_pulse = None             # time.time() of the last pulse sent

    @property
    def enabled(self):
        return bool(self.config['enabled'])

    def configure(self, config):
        """New settings (ValueError when they are wrong: the old ones stay). The pin is let go when it is no longer the same pin"""
        values = checked(config)
        with self._lock:
            same_pin = all(values[key] == self.config[key] for key in ('chip', 'pin', 'active_low'))
            if not same_pin or not values['enabled']:
                self._close_pin()
            self.config = values
            if not values['enabled']:
                self.error = None          # an old error says nothing about a dropper that is off
        return values

    def pulse(self, reason='', gate=None):
        """
        Release one card: the pin active for pulse_ms, then inactive again. True when it was sent; False when the dropper is off,
        a pulse is still under way, the gate said no, or the pin cannot be used (then `error` says why). Never raises: a dropper
        that fails must not stop the scanning, which goes on by hand.
        gate: the caller's last word, a context manager factory whose value says whether the card is still wanted. It is entered
        AFTER the line is opened and the pin is set active INSIDE it: the caller holds its own lock there, so whatever could cancel
        the release (scanning stopped, the dropper switched off) happens wholly before the check - refused - or after the pin is
        active - the card already on its way (a Codex review of 2026-10-22: a check, then the line opened, then the pin, left a
        gap a stop and restart fell into: two cards, no capture between).
        """
        if not self.enabled:
            return False
        if not self._lock.acquire(blocking=False):
            logger.warning(f"Card dropper: still releasing the previous card - not another one ({reason})")
            return False
        try:
            # Again, now under the lock configure() changes the settings under: switched off between the check above and here,
            # nothing is opened or activated (a Codex review of 2026-10-22). From here on the settings cannot change
            if not self.config['enabled']:
                return False
            if self._pin is None:
                try:
                    self._pin = self._open_pin(self.config['chip'], self.config['pin'], self.config['active_low'])
                except Exception as e:
                    self._fail(e)
                    return False
            try:
                if gate is None:
                    self._pin.set(True)
                else:
                    with gate() as wanted:
                        if not wanted:
                            logger.info(f"Card dropper: no longer wanted at the last moment - no card released ({reason})")
                            return False          # (the finally below sets the pin inactive: it already is - harmless)
                        self._pin.set(True)
                self._sleep(self.config['pulse_ms'] / 1000)
            finally:
                # Inactive again whatever happened: a pin left active would hold the mechanism open
                try:
                    self._pin.set(False)
                except Exception as e:
                    self._fail(e)
                    self._close_pin()
                    return False
            self.pulses += 1
            self.last_pulse = time.time()
            self.error = None
            logger.info(f"Card dropper: card released ({reason}) - {self.pulses} since the start")
            return True
        except Exception as e:
            self._fail(e)
            self._close_pin()
            return False
        finally:
            self._lock.release()

    def state(self):
        """What the server shows on the Settings page"""
        return {'enabled': self.enabled, 'pulses': self.pulses, 'error': self.error, 'last_pulse': self.last_pulse}

    def close(self):
        with self._lock:
            self._close_pin()

    # -- inside ---------------------------------------------------------------------------------------------------------------

    def _fail(self, error):
        message = str(error) or error.__class__.__name__
        if message != self.error:          # said once, not at every card
            logger.error(f"Card dropper: {message}")
        self.error = message

    def _close_pin(self):
        if self._pin is not None:
            self._pin.close()
            self._pin = None
