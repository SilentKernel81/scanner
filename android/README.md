# Card Scanner - Android (Magic only, first version)

Native Kotlin app (Jetpack Compose + CameraX + OpenCV): the phone's camera finds the card
by its outline, captures it once it is still, has a vision AI read name / collector number /
set code / ★• foil marker, and looks up the exact printing on the Scryfall API. No inventory yet.

Ported from the Python scanner - keep both in step:

| Android | Python |
|---|---|
| `detection/CardOutline.kt` | `object_detector.py` (`find_card_outline`, `warp_card`) |
| `detection/CardTracker.kt` | `scanner.py` (`_is_card_settled`, `_new_card_arrived`, `_outline_flicker`; fixed area: `_fixed_area_step`, same thresholds) - no focus sweep: the phone's continuous autofocus |
| `ai/CardIdentifier.kt`, `ai/Prompts.kt` | `card_identifier.py`, `prompts.py` (`BUILT_IN['mtg']`) |
| `scryfall/Scryfall.kt` | `database.py:search_card_exact` (online, same `match` tags) |
| `inventory/Inventory.kt`, `inventory/Export.kt` | `inventory.py` (same table and merge key; confirmed cards added automatically like `auto_add`), `games/mtg.py` (`write_csv` - byte-identical, `ExportParityTest`; Moxfield) |

## Build and install

Needs Android Studio's JDK and SDK platform 37.2.

```bash
export JAVA_HOME=/opt/android-studio/jbr
./gradlew assembleDebug                      # app/build/outputs/apk/debug/app-debug.apk
~/Android/Sdk/platform-tools/adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Or open `android/` in Android Studio and Run. In the app: Settings -> provider, model and API
key, or the local server address (`http://<host>:11434` for Ollama; "Test / list models").

## Tests

- `./gradlew testDebugUnitTest` - AI answer parser and name similarity against the Python
  results (`app/src/test/resources/parser_reference.json`, made by `parser_reference.py`); CSV
  export against `write_csv` (`export_reference.json`, made by `export_reference.py`).
- `OutlineParityTest` (instrumented) - outline detection over recorded frames; compare the
  output with `find_card_outline` on the same frames. Frames are passed in as assets:
  `./gradlew assembleDebugAndroidTest -PdebugFrames=<dir containing frames/>`, install both
  APKs, `adb shell am instrument -w -e class com.cardscanner.OutlineParityTest com.cardscanner.test/androidx.test.runner.AndroidJUnitRunner`,
  then `adb pull /sdcard/Android/data/com.cardscanner/files/outlines.json`.
  (Measured 2026-09-26 on 400 frames of data/debug_frames: same detections as Python, corners within 0.6 px.)
- `PipelineTest` (instrumented) - card images (assets `cards/`) -> local AI -> Scryfall:
  `-e aiUrl http://host:11434 -e aiModel <model>`; writes `pipeline.json`.

## Not done yet

- Thresholds (sharpness 250, movement 1%, ...) were measured on the Pi webcam; re-measure on
  phones (live values are shown under the camera; min sharpness and still frames are in Settings).
- Tested on a Pixel 10 over the box. Auto + fixed area, 2026-09-26: 13 cards dropped in 33 s, each
  captured once, all 13 matched by set + number or name + number. Outline mode not measured in auto yet.
- Moxfield export writes the set code as Edition (Moxfield matches it; the Python export writes
  the set name, which Moxfield doesn't recognize).
- Correcting a wrong printing (search / printing picker), CSV import, local card database (offline), Pokémon, prompt editor, API keys in the Android Keystore, per-ABI APKs (debug APK is ~110 MB).
