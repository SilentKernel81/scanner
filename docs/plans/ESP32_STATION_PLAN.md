# ESP32 camera station with a scale: plan

Date: 2026-10-10

A plan for a small camera station built on an ESP32-S3 with an autofocus camera and a load
cell under the box: the weight of a card landing triggers the capture, the photo is the fixed
area, and the server does everything else. Nothing here is implemented and nothing has been
measured: no part has arrived yet. The statements about the hardware below come from product
pages, driver documentation and forum threads read on 2026-10-10, and the pixel figures are
geometry, not photographs. Phases 0 and 1 exist to check them before anything is built.

## The idea

Today a station decides from the camera alone that a new card has landed
(`scanner.py: _new_card_arrived`, in fixed-area mode `_fixed_area_step`). A scale gives a
second signal that does not depend on what the card looks like: each card is a step of about
1.8 g in the pile's weight.

What the weight step would settle, all of them open points in ../PROGRAM_DOCUMENTATION.md today:

| Case | Camera today | With the scale |
|---|---|---|
| An identical copy landing within ~0.7 mm of the previous card | Not recognized, press Capture (*Known limitations*) | One step: a new card |
| A hand over the box for ~0.3 s, same card still on top | Captured again | No step: no new card |
| A focus probe read as a drop; a card shifting ~2% after its capture | Duplicates, 2 of ~630 captures | No step: no new card |
| Two cards dropped together | Cannot be seen | A step of ~3.6 g: warn that a card is hidden |
| A card taken back off the pile | Looks like a change of card | A negative step |

The scale only says that something landed. That the card lies still, the hand is gone and the
picture is sharp still has to be checked on the picture.

## Why an ESP32 becomes possible

An ESP32 cannot run today's station: no OpenCV, a few MB of memory, about a second to decode
one full-size JPEG. With the scale as the trigger and the **fixed area** as the photo it does
not have to find the card's outline at all:

| Job | Today (`_fixed_area_step`) | ESP32 station |
|---|---|---|
| New card | The area changed sharply, or settled looking different | Weight step of one card |
| Card present | Sharpness of the area | Weight above the empty box |
| Still, hand gone | 160 px thumbnail compared frame to frame | Weight settled, plus the same thumbnail comparison on frames decoded at 1/8 scale (speed not measured) |
| The photo | The area as drawn | The whole frame uploaded; the server crops the station's saved fixed area |
| Flat card for the ★/• foil check (`foil_image`) | Outline detector on the station | Outline detector on the server, on the uploaded frame |
| Focus | Sweep, lock, drift probes on `focus_absolute` | The camera's own autofocus, triggered every ~10 cards (the scale counts them) and after a large weight change |

What is given up: the live view on the camera's page (at best a small, slow one), the focus
sweep and its drift tracking, and capture without a scale. The Android phone cannot have a
scale, so the scale stays optional per station and the camera rules stay as they are for the
other stations.

## Parts

| Part | Choice | Why / what to check |
|---|---|---|
| Board | Waveshare ESP32-S3-CAM-OV5640 (ESP32-S3R8, 8 MB PSRAM, 16 MB flash) | Full-resolution JPEG needs PSRAM. 24-pin DVP camera connector, a UART and an I2C header. Microphones, speaker, display connector and battery charging are not used |
| Camera | OV5640 AF, 70° lens, 24-pin 0.5 mm flex, the long flex | Autofocus, because the pile rises towards the lens. Confirm the 70° is the diagonal. Replaces the board's bundled OV5640, which is almost certainly fixed focus (the listing never says "AF") and stays as the fallback |
| Scale | 1 kg load cell + HX711 | A full pile of ~500 cards is ~900 g. Two GPIO pins, on the UART header. A NAU7802 (real I2C) is the alternative if those pins are not free |
| Cross-check (optional) | Classic ESP32-CAM | The Arduino autofocus library was written for it with this kind of module: tells the module from the board when the lens does not move |

Rejected: the classic ESP32-CAM's OV2640 (2 MP, fixed focus, soft); a 120° autofocus module
(about 4.7 px per mm at 20 cm, the collector line ~7 px tall); the Seeed XIAO ESP32S3 Sense as
first choice (ships with an OV2640 / OV3660, and a thread on Espressif's forum reports the
OV5640's autofocus not working on it, without a resolution). The Adafruit MEMENTO is the
alternative with autofocus known to work, at about twice the price.

Pixels on the card (geometry: card standing along the frame's short side, 1944 px, lens angle
taken as the diagonal):

| Lens | Camera 20 cm above the card | Collector line (~1.5 mm) |
|---|---|---|
| 68° - 70° | ~12.6 px per mm | ~19 px |
| 120° | ~4.7 px per mm | ~7 px |

## Open points

1. **Does the Waveshare board power the focus motor?** Its pages do not mention autofocus and
   the camera connector's pins are only in the schematic (linked from the board's *Resources
   and Documents* page). Read the schematic, or plug the module in and send a focus command.
2. **Can the collector line be read from this camera's pictures?** Decides whether to go on.
3. **Does autofocus lock on the top card** (not the box wall, not a foil's glare), over the
   pile's whole height, and how long does one run take?
4. **Lens position cannot be set directly** as far as found: the driver offers "focus now" and
   "wait until done". Setting the lens from the pile's height (weight → count → height → lens
   position) would need the sensor's focus-motor registers - not looked into.
5. **The scale's signal**: the spike and ringing of a drop and how long they last (it has to
   fit into the ~1.6 s between captures); noise at 10 and 80 readings a second; a hand on the
   box edge, a bumped table, a draught.
6. **What "one card" weighs**: sleeved cards, foils and tokens differ, so it is a range. A step
   outside it is a warning, not a capture.
7. **Scale changes with the pile.** A camera mounted so a card on a 15 cm pile just fits sees a
   card on the empty box at about half the size (collector line ~13 px with the 70° lens).
   Capping the pile at a few centimetres (~100 cards) keeps it near 17 px.
8. **Heat**: OV5640 modules are sold with heat sinks; behaviour over a long session at full
   resolution is unknown.

## Phases

### 0. The camera alone (decides whether to continue)

A minimal sketch on the board: focus, take one full-resolution JPEG, serve it over HTTP.

- Does the lens move on a focus command? If not: schematic, then the cross-check board.
- Photograph the same 20 cards on the empty box and on top of a tall pile (include a foil, a
  sleeved card, a white-bordered card, an old printing).
- Run the pictures through `CardOcr.read_card` + `Game.confirmed_read` on a **copy** of the
  database and compare with the same cards from the current webcam station.
- Note the time for one focus run and one capture, and the board's temperature after 15 min.

Stop here if the collector line does not read reliably: a Raspberry Pi Zero 2 W with a Camera
Module 3 (12 MP, autofocus) and the HX711 on its GPIO pins would be the small station instead,
on the existing station code (untested too).

### 1. The scale alone

Load cell and HX711 on the board (or on the Pi station, whichever is ready first), the box on
the platform, raw readings logged with timestamps while dropping about 50 cards: singles,
identical copies, a double drop, a card taken back, sleeved cards, a hand over the box without
dropping, a hand resting on the edge, the table bumped.

From the log: the step per card and its spread, the settling time after a drop, the noise of a
resting pile at both sample rates, and whether one and two cards separate cleanly at 0, 100
and 500 cards on the pile. The thresholds come from this log, as the camera's did from its
noise measurements and drop test.

### 2. The server's side

- A full frame as the upload: crop the station's saved fixed area for `image`, run
  `find_card_outline` + `warp_card` on the frame for `foil_image`. The rest of
  `station_capture` → `submit_capture` stays (`capture_id`, counted once).
- Refuse a capture whose area is not sharp or shows no card, with an answer the station can
  act on (take it again).
- A station that has no live view and no focus sweep: what its page shows, which
  `RemoteScanner` attributes and methods it does not have.
- Decide whether this station speaks the existing `/station` Socket.IO protocol or only the
  HTTP upload plus a small status call.

### 3. The firmware

Its own program (C++, esp32-camera driver): scale loop (step, settle, count), stillness check
on small decoded frames, autofocus every N cards, capture, upload with `capture_id` and retry
until the server answers ("at least once"), status to the server, settings from the server
(fixed area, add automatically). A third station implementation beside `station_client.py`
and the Android app - its README should map each part to the Python code, as the Android
app's does.

### 4. The scale on the existing stations (independent of the ESP32)

An optional weight input to `_new_card_arrived` / `_fixed_area_step` in `scanner.py` for the
Pi station (HX711 on GPIO): no step, no new card; a double step, a warning. This fixes the
duplicate and identical-copy cases for the stations that already work, and can be done as
soon as phase 1 has its numbers.

### 5. Measure and document

A drop test like the camera's (every card captured exactly once, identical copies, doubles
reported), then ../PROGRAM_DOCUMENTATION.md (the station, the thresholds and how they were
measured, what was never tested), README.md, ../INSTALL.md (wiring, flashing), ../TEST_CASES.md.

## Order of work

1. Parts arrive → phase 0 (camera) and phase 1 (scale) - both are small scripts and logs.
2. With their results: decide ESP32 or Pi Zero, and whether phase 4 goes first (it helps the
   current stations whatever the ESP32 turns out to be).
3. Phases 2 and 3 together, then 5.

## Sources read

- Waveshare ESP32-S3-CAM-OV5640: https://www.waveshare.com/esp32-s3-cam-ov5640.htm,
  https://docs.waveshare.com/ESP32-S3-CAM-OVxxxx
- OV5640 autofocus on the ESP32: https://reference.arduino.cc/reference/en/libraries/ov5640-auto-focus-for-esp32-camera,
  esp32-camera's `esp_camera_af.h`; writing sensor registers: https://github.com/espressif/esp32-camera/issues/506
- Autofocus not working on the XIAO ESP32S3 Sense: https://esp32.com/viewtopic.php?p=121562
- Adafruit MEMENTO (focus motor power jumper): https://learn.adafruit.com/adafruit-memento-camera-board/pinouts
