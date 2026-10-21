# OCR on the phone: the light-ocr reader ported to Android

*Plan, 2026-10-21. Nothing of it is built yet. Written for the sessions that will build it: every
step says what to make, what to run, and what has to be true before the next step starts. Read
"Rules for the sessions that build it" at the end first.*

## What it is for

The server reads a card with **light-ocr** first (`core/card_ocr.py` starts `src/ocr/server.mjs`,
a Node.js process) and asks the vision AI only for what OCR cannot confirm: 88.1% of 747 saved scans
were confirmed by OCR alone (docs/PROGRAM_DOCUMENTATION.md, "OCR first"). The Android app has no
OCR: standalone it asks the vision AI for every card (cost, a network, 1-2 s), and its port of the
reading by picture (`reading/Decide.kt`, `Fingerprint.kt`, `FoilNet.kt`) is **not wired in**, partly
because "finding the set line needs the server's OCR reader, which the phone does not have"
(`../mtg-scanner-android/README.md`).

After this plan the phone has the same reader as the server: the same two models, the same
pre- and post-processing, the same parsers, checked line by line against the server's reader on
the same pictures. It gives the phone, in this order of use:

1. the **OCR-first** reading in standalone mode (name, collector number, set code, ★/• marker ->
   a confirmed printing is added without the AI; the rest goes to the AI as today);
2. the **strip read** (`parse_strip`, `symbol_read`) and the **set line's box** (`set_line_box`)
   that the picture decision and the foil network need - the pieces `reading/` lacks today;
3. the lines of the whole card for the OCR guard of a picture-alone decision (`_ocr_names`,
   `printing_check.ocr_facts`) - made possible, not wired in here.

## What light-ocr is (looked at 2026-10-21, `src/ocr/node_modules/@arcships/`)

"Rewriting light-ocr" is smaller than it sounds. The npm package is three layers:

| Layer | What it is | Size |
|---|---|---|
| `server.mjs` + the package's JavaScript | glue: one JSON request per line on stdin, loading, a worker thread | 35 lines + 72 KB |
| **Two ONNX models** of PaddleOCR **PP-OCRv6 "small"** (`light-ocr-model-ppocrv6-small/bundle/`): `det/inference.onnx` (text detection, the DB algorithm), `rec/inference.onnx` (text recognition, CTC), `rec/dictionary.json` (18,709 characters, 15,565 of them CJK, the last one a space) | the reading itself | 9.9 MB + 21.2 MB fp32 (the `webgpu/` pair is fp16 for GPUs: 15.6 MB, not for a phone's CPU) |
| A native C++ core (`light_ocr_node.node`: onnxruntime + OpenCV core/imgproc + Clipper), Apache-2.0, source public at github.com/arcships/light-ocr | resize, normalise, DB post-processing, perspective crop of every line, CTC decoding - the standard PP-OCR recipe | 11 MB |

The bundle ships the exact recipe as data: `bundle/normalized-config.json`. It is the specification
of this port (quoted in "The pipeline" below). Nothing in it is light-ocr's own invention; PP-OCR's
pipeline has existing Android ports (RapidOCR's, PaddleOCR's own demo), which may be looked at for
the shape of the code but are not copied.

The models' own `det/inference.yml` names other thresholds (0.2 / 0.45 / 1.4) than light-ocr uses
(0.3 / 0.6 / 1.5). **The port follows light-ocr's `normalized-config.json`**, because parity with
the server's reader is what is measured; the yml is the training-time default.

## What exists already

| Need | Where it is today |
|---|---|
| Running an ONNX network on the phone | `detection/CornerNet.kt`, `reading/FoilNet.kt`: OpenCV dnn (`Dnn.readNetFromONNX` on the asset's bytes, `net.forward`, `synchronized(net)` because a dnn network is not safe from two threads). OpenCV 4.14 is in the app (`libs.versions.toml`). |
| The parsers | `core/card_ocr.py`: `parse_magic(lines, width, height)`, `parse_strip(lines)`, the regular expressions `MAGIC_NUMBER`, `MAGIC_NUMBER_OF_TOTAL`, `MAGIC_SET`, `DIGIT_LOOKALIKES`, `STARS` / `DOTS`, `prints_token_letter`, `token_set_code`. About 85 lines of regex over the lines. |
| The strip and the set line | `core/foil_marker.py`: `STRIP`, `STRIP_CARD_HEIGHT`, `strip_of`, `set_line`, `set_line_box`, `symbol_read`, `set_line_crop`, `MARGIN`; `core/printing_check.py`: `read_strip_evidence` (what one read of the strip gives: `{'number', 'set', 'symbol', 'net'}`). |
| The line format the parsers take | `server.mjs`: `[{"text", "confidence", "box": [[x, y] x 4]}]`, the box's corners in image pixels starting top-left, clockwise; `CardOcr.read_lines` returns it unchanged. |
| The pattern of a port with a parity test | `reading_reference.py` (in `../mtg-scanner-android/app/src/test/resources/`, run from this checkout with its venv) makes the reference files from the server's own code; `androidTest/FoilNetParityTest.kt` checks the network file's SHA-256 and the answers on an emulator; JVM tests check the pure arithmetic. |
| Pictures with known answers | `~/.cache/mtg-image-match/truth/`: `rescan_cards/` (41 phone captures, `phone-0240eb84_*.jpg`, 1015 x 1419), `captures/`, `captures_full/`, `scanned_cards/`, with `captures.json` / `hard_truth.json` (the image-match experiment's truth); this checkout's `scanned_cards/` (3,499 station captures, no labels). Read only - never delete or move any of them. |
| light-ocr itself, to compare with | `node server.mjs cpu` in `src/ocr/` (the GPU provider crashed on this machine - `data/logs/ocr.log`; use `cpu` in scripts). 0.6 s per card on this CPU, 10 lines on a phone capture. |

## Measured before planning (2026-10-21, this machine)

- **Both ONNX files load and run in OpenCV's dnn module** (Python OpenCV 5.0.0, `readNetFromONNX`),
  with **dynamic input sizes**: det 1x3x640x480 / 736x544 / 960x704 -> a probability map of the
  same size (`(1, 1, H, W)`, 0.06-0.14 s); rec 1x3x48x320 -> `(1, 40, 18710)`, 48x640 -> `(1, 80,
  18710)`, a batch of 4 at 48x1280 -> `(4, 160, 18710)`. The recognizer's output is a softmax
  (rows sum to 1): 18,710 classes = blank (index 0) + the 18,709 dictionary characters, one time
  step per 8 pixels of width. **Not measured: the same in the app's OpenCV 4.14 on Android** - step 0.
- light-ocr on one phone capture (`phone-0240eb84_1030_*.jpg`): 10 lines, 0.57-0.61 s per request
  on the CPU; the three lines the parsers use came out as `'Madame Hydra'`, `'U 0318'`,
  `'MSH★EN ANNIE WU'` (the star is read: the dictionary has ★ • · * ☆ ●).

## Design

### Route: OpenCV dnn, the same two ONNX files, the pipeline in Kotlin

Three routes were weighed. **Chosen: Kotlin on OpenCV dnn**, which the app already has and which
runs both models on the development machine. The alternatives, kept as fallbacks:

- *onnxruntime-android* (Microsoft's AAR, ~12 MB per ABI; NNAPI / XNNPACK acceleration): the same
  Kotlin pipeline with the two `forward` calls replaced. Taken only if step 0 fails or step 5
  measures the dnn path too slow. `TextReader` keeps inference behind one small interface
  (`Inference.run(det|rec, blob)`) so that this swap touches one file.
- *Compiling light-ocr's C++ core with the NDK*: exact parity for free, but its build is made for
  desktops (Node-API, WebGPU, NPU runtimes), Android appears nowhere in it. Not first.
- *Google ML Kit text recognition*: an afternoon, but a different reader (no ★/•, other line
  geometry), the parsers' measured 88.1% would not carry over, and it breaks the rule that the
  Kotlin mirrors the Python with parity tests. Not taken.

### The pipeline (the specification; every number from `normalized-config.json`)

The input is the **flat card** as the app has it after `cutOutCard` (the server reads the same:
the perspective-corrected capture), RGB. OpenCV's dnn wants planar float32 `[N, 3, H, W]`.

**1. Detection.**
- Resize: the longer side to at most **960** (scale down only; a smaller picture keeps its size;
  the shorter side at least 64), then each dimension rounded **up** to a multiple of 32
  (`dimensionMultipleRounding: ceil`). A 1015 x 1419 capture becomes 704 x 960. Linear
  interpolation. Remember the two ratios (original / resized, per axis) for the boxes.
- Normalise: channel order **BGR** (`colorOrder: BGR` - PP-OCR's detector is fed OpenCV's BGR as
  is), `(pixel / 255 - mean) / std` with mean `[0.485, 0.456, 0.406]`, std `[0.229, 0.224, 0.225]`
  applied to the channels in that BGR order, as the config lists them.
- Forward -> probability map `[1, 1, H, W]`.
- **DB post-processing** (`algorithm: DB`, `boxType: quad`, `scoreMode: fast`): the bitmap is
  `map > 0.3` (`threshold`); `findContours` on it (RETR_LIST, CHAIN_APPROX_SIMPLE), at most 3000
  candidates; for each contour: `minAreaRect` -> 4 points; a shorter side below **3** px
  (`minimumBoxSide`) is dropped; the box score is the **mean of the probability map inside the
  4-point polygon** (fast mode); below **0.6** (`boxThreshold`) dropped; **unclip** by ratio
  **1.5**: PaddleOCR offsets the polygon by `distance = area * 1.5 / perimeter` with round joins and
  takes `minAreaRect` of the result - for a quad that is exactly **the same rotated rectangle grown
  by `distance` on every side** (no Clipper needed; a JVM test pins it against numbers from the
  Python reference); a grown box whose shorter side is below 3 + 2 px is dropped; the points are
  scaled back to the original picture by the two ratios, clipped to it, and **ordered clockwise
  from the top-left** (PaddleOCR's `order_points_clockwise`: the two with the smallest x are left,
  of those the smaller y is top-left).
- Lines are sorted in reading order: by the top-left y, lines whose y differ by less than
  **10 px** (`rowBandPixels`) by x.

**2. Recognition**, one line at a time (`runtimeDefaults.recognitionBatchSize: 1`; batching up to 8
sorted by width is allowed by the config but not needed).
- Crop: a perspective transform of the quad to an upright rectangle of width `max(|top edge|,
  |bottom edge|)` and height `max(|left edge|, |right edge|)` (PaddleOCR's `get_rotate_crop_image`),
  **cubic** interpolation, **replicate** border. A crop taller than **1.5** times its width
  (`tallLineRatio`) is turned **90 degrees counter-clockwise**.
- Resize to height **48**; width `ceil(48 * w / h)` (`resizedContentWidthRounding: ceil`), at most
  **3200**; the tensor is at least **320** wide (`minimumTensorWidth`), the rest padded with
  **0.0** after normalisation (`paddingValue`). Linear interpolation.
- Normalise: **BGR**, `(pixel / 255 - 0.5) / 0.5`.
- Forward -> `[1, T, 18710]` softmax, T = width / 8.
- **CTC decode**: per time step the argmax; drop index 0 (blank); collapse a repeat of the previous
  step's index; character = `dictionary[index - 1]` (the dictionary's last entry is the space -
  `appendSpaceCharacter` is already done in the file); **confidence = the mean of the argmax
  probabilities of the kept steps** (`mean_selected_argmax_probability`), 0 for an empty line.
  Lines with an empty text are dropped (`defaultScoreThreshold: 0.0` otherwise keeps everything).

**3. The answer**: `List<TextLine(text, confidence, box: 4 points in the input's pixels)>` - the
JSON of `server.mjs` as a Kotlin type.

**4. The parsers** are ports of `card_ocr.py` and `foil_marker.py` as they are: the same regular
expressions (Kotlin's `Regex` takes them unchanged; `[^A-Za-z0-9\s]` and `À-ÿ` behave the same),
the same positions (title: top < 0.16 and left < 0.35; bottom lines: top > 0.84 and left < 0.6;
`STRIP` = rows 0.86-1.0, columns 0-0.62, enlarged to a 1000 px card), the same order of the two
bottom lines, the token letter `T`, lower case and stripped zeros for the strip's answer.

### Files

Android (`../mtg-scanner-android`, package `com.cardscanner.reading`):

| File | What |
|---|---|
| `reading/TextReader.kt` | `TextReader.load(context): TextReader?` (both networks from the assets, null when one cannot be read - the caller then has no OCR, as `CornerNet.load`); `read(rgb: Mat): List<TextLine>`; `close()`. Holds the two `Net`s under `synchronized`. The two forwards behind `private interface Inference`. |
| `reading/TextDetection.kt` | the pure arithmetic of step 1 after the forward: `boxesOf(map: FloatArray, w, h, ratioX, ratioY): List<Quad>`, `grow(quad, ratio)`, `orderClockwise`, `readingOrder`. JVM-testable (plain arrays, no `Mat` except through OpenCV's Java API where unavoidable: `minAreaRect` and `findContours` need `Mat`s, so this file is tested on the emulator; the parts that are plain maths - `grow`, `orderClockwise`, `readingOrder`, `scoreOf` on an array - in `TextMath.kt`, JVM-tested). |
| `reading/TextRecognition.kt` | the crop geometry (`cropSize(quad)`, `isTall`), the input width rule (`tensorWidth(w, h)`), `ctcDecode(probabilities: FloatArray, steps, classes, dictionary): Pair<String, Float>`. Pure, JVM-tested. |
| `reading/CardText.kt` | the parsers: `parseMagic(lines, width, height): OcrRead?`, `parseStrip(lines): Pair<String, String>`, `setLine(lines)`, `setLineBox`, `symbolRead(line)`, `stripOf(card: Mat)`, `setLineCrop(strip, box)`; the constants of `card_ocr.py` / `foil_marker.py` by their Python names in comments. Pure except the two `Mat` helpers. |
| `assets/ocr/det.onnx`, `assets/ocr/rec.onnx`, `assets/ocr/dictionary.json`, `assets/ocr/LICENSE-PaddleOCR.txt` + `NOTICE` | copies of the bundle's fp32 files and the model notice (Apache-2.0: the notice travels with the models). +31 MB in the APK (the release is ~41 MB today) - accepted for now; a download like the card data is a later option, noted in README. |
| `androidTest/TextReaderParityTest.kt` | on the emulator: the asset hashes, the lines on the parity pictures against the Python reference, the parser results against light-ocr's (see Testing). |
| `test/.../TextMathTest.kt`, `TextRecognitionTest.kt`, `CardTextTest.kt` | JVM: the pure parts against `ocr_math_reference.json`, `ocr_parser_reference.json`. |
| `app/src/test/resources/ocr_reference.py` | makes every reference file above from this checkout (as `reading_reference.py` does), copies the model files. |

Server (`scanner-server`, nothing the server runs changes):

| File | What |
|---|---|
| `scripts/experiments/ocr_port/ppocr_reference.py` | **the Python reference of the pipeline**: the same steps on `cv2.dnn`, every constant read from the bundle's `normalized-config.json` by name, `read_lines(rgb) -> lines` in `server.mjs`'s format. The executable version of "The pipeline" above; the Kotlin is a port of it. |
| `scripts/experiments/ocr_port/compare_with_light_ocr.py` | runs both readers over a folder of pictures: line texts matched by box overlap, the parser's answer on each, a report of every difference (see step 1). |
| `scripts/experiments/ocr_port/README.md` | what was measured, with the numbers (the project marks measured against never tried). |

### Where it fits on the phone (step 6, behind a setting)

Standalone, in `CaptureProcessor.identifyOnce`, before the AI, the cascade half of the server's
`Identification._read_with_ocr` (its `reading.mode: cascade` order - the picture-first order needs
the index on the phone, which is not in this plan):

1. `TextReader.read(card)` -> `CardText.parseMagic` -> an `OcrRead` or nothing (no name read).
2. `Scryfall` lookup as the server's `Game.confirmed_read` (the app's `scryfall/Scryfall.kt` has
   the same steps and `CONFIRMED` tags): a confirmed match is added like an AI answer is today,
   with `reader = "light-ocr"` where the AI's model name goes (the server writes exactly that in
   `scanned_cards.log`; the phone's `ServerOutcome.readerLabel` already knows the label).
3. The foil marker: the symbol OCR read; unread -> `FoilNet` on `setLineCrop(stripOf(card),
   setLineBox(strip lines))` (one more `read` of the strip, as `read_strip_evidence` does), sure
   above `SURE` = 0.9; still unknown -> the AI's foil check as today.
4. Not confirmed -> the AI as today, the OCR read kept for the review item's suggestion (the
   server does this: "it usually still finds the card by name for review").

Setting: *Read with OCR first* (Settings -> Standalone), **off by default until step 5 has
measured it on a phone**; client mode is untouched (the server reads).

## Steps

Each step ends with a check that must pass before the next. Commit per step when the user asks.

### Step 0 - Feasibility on the emulator (half a day at most)

1. In `../mtg-scanner-android`: copy `det/inference.onnx`, `rec/inference.onnx` from
   `../scanner-server/src/ocr/node_modules/@arcships/light-ocr-model-ppocrv6-small/bundle/` to
   `app/src/main/assets/ocr/det.onnx` and `rec.onnx`.
2. An instrumented test `OcrModelsLoadTest` (temporary; it becomes part of
   `TextReaderParityTest` in step 3): `Dnn.readNetFromONNX` on both; a forward of det at
   1x3x704x960 and 1x3x64x64; of rec at 1x3x48x320 and 1x3x48x640; assert the output shapes above.
   Log the time of each forward.
3. Build and run on the `Pixel_Phone` emulator (the commands in the app's CLAUDE.md: `assembleDebug
   assembleDebugAndroidTest`, `adb shell am instrument -w -e class ...`).

**Done when** both networks load and give the expected shapes at both sizes in the app's OpenCV
4.14. **If the recognizer fails** (an unsupported operator, a wrong dynamic shape): the route is
onnxruntime-android - add `com.microsoft.onnxruntime:onnxruntime-android` to `libs.versions.toml`,
and `Inference` in step 3 is written on it; everything else in this plan stays.

### Step 1 - The Python reference, checked against light-ocr (1-2 days)

1. Write `scripts/experiments/ocr_port/ppocr_reference.py` (`venv/bin/python`, the venv's OpenCV
   5.0): `class Reader` with `read_lines(rgb)`; every constant from the bundle's
   `normalized-config.json`, looked up by its key (a change of bundle fails loudly); the pipeline
   exactly as specified; a `--trace` option that also returns the intermediate values a Kotlin
   test wants (the resized size, the raw boxes before and after growing, each crop's size and
   tensor width, each line's argmax indices).
2. Write `compare_with_light_ocr.py <folder> [--limit N] [--out report.json]`: starts
   `node server.mjs cpu` once (as `card_ocr.CardOcr` does; reuse its protocol), reads every picture
   with both, pairs lines by box overlap (IoU >= 0.5), and reports per picture: lines only one
   reader found, paired lines whose text differs, and the four fields of `parse_magic` from each
   reader's lines, plus `parse_strip` and `symbol_read` on the strip (`foil_marker.strip_of`).
3. Run it on `~/.cache/mtg-image-match/truth/rescan_cards/` (41 phone captures: the phone is the
   target) and on 200 of `~/.cache/mtg-image-match/truth/captures/` (station captures; the truth
   files say what each card is). Iterate on the reference until the differences are understood.

**Done when**, on the 241 pictures, `parse_magic` on the reference's lines gives the same
(name, collector_number, set_code, foil) as on light-ocr's lines for **at least 98%**, and every
remaining difference is explained in the README (a line light-ocr split differently, a box score
on the threshold). Record the numbers there: identical parses, line texts identical, boxes within
N px, time per picture for each. **Not** required: identical boxes to the pixel - light-ocr's C++
differs from Python OpenCV in sub-pixel ways; the texts and the parses are what the phone needs.

`★` *Why a Python reference first: the project ports Python to Kotlin with a reference file made by
the Python (`CornerNet`, `Fingerprint`, `FoilNet`); a Node + C++ reader cannot make one. The Python
reference is where the pipeline is got right cheaply, and the Kotlin is checked against it exactly
(same library, same numbers) and against light-ocr loosely (texts, parses).*

### Step 2 - Reference files and the pure Kotlin (1 day)

1. `app/src/test/resources/ocr_reference.py` (run from this checkout:
   `venv/bin/python ../mtg-scanner-android/app/src/test/resources/ocr_reference.py`), writes:
   - `app/src/main/assets/ocr/det.onnx`, `rec.onnx`, `dictionary.json`, the licence files - copies;
   - `app/src/test/resources/ocr_math_reference.json`: for a dozen quads (hand-chosen: a flat line,
     a tilted one, a tall one, a tiny one, one touching the picture's edge) the grown quad, the
     clockwise order, the crop size and `isTall`, the tensor width; three reading-order cases; six
     CTC cases (a plain word, repeats, blanks between repeats - `aa` must survive as `aa` only
     with a blank between -, the space, an empty line, the confidence);
   - `app/src/test/resources/ocr_parser_reference.json`: light-ocr's real lines of 30 pictures
     (texts and boxes, from step 1's report) with `parse_magic`, `parse_strip`, `set_line_box`,
     `symbol_read` on them, computed by the Python functions themselves (`card_ocr`, `foil_marker`);
     include the token case (`T 0001` over `FRA • EN` -> `TFRA` / `tfra`), a `123/281` card, a
     card with `O172`-style look-alikes, a name with stray mana symbols, a foil star and a dot;
   - `app/src/androidTest/assets/parity/ocr/*.png` + `ocr_lines.json`: 6 pictures (4 phone
     captures from `rescan_cards`, 2 station captures), as PNG (a JPEG decoder on Android gives
     other pixels), each with the Python reference's lines and its `--trace` values, and
     light-ocr's lines, and the SHA-256 of the three asset files.
2. `TextMath.kt`, `TextRecognition.kt`, `CardText.kt` with their JVM tests against the two
   JSON files. `./gradlew testDebugUnitTest` passes.

**Done when** the JVM tests pass and `CardText` gives the Python's answer on all 30 pictures'
lines (exact strings).

### Step 3 - `TextReader` on the emulator, line parity (1-2 days)

1. `TextDetection.kt`, `TextReader.kt` as in Files; the `Inference` interface with the dnn
   implementation. Generous comments: which Python line each block is.
2. `TextReaderParityTest` (emulator only): (a) the asset hashes equal the reference's; (b) on each
   parity PNG, the resized size and tensor widths equal the trace; the lines equal the Python
   reference's: **same count, same texts, boxes within 2 px per coordinate, confidence within
   0.02**; (c) `parseMagic` / `parseStrip` / `symbolRead` on the Kotlin lines equal the Python's
   on light-ocr's lines for the 6 pictures; (d) `read` from two threads at once does not crash.
   Log the time per picture (`Log.i("Scanner", ...)`, as `FoilNetParityTest` does).

**Done when** (a)-(d) pass on the x86_64 emulator. Tolerances may be loosened only with the
difference explained in the test's comment (OpenCV 4.14 on Android against 5.0 on the desktop is
the likely source), never past "same texts".

### Step 4 - The strip and the foil network together (half a day)

`CardText.stripOf` + `TextReader.read(strip)` + `setLineBox` + `setLineCrop` + `FoilNet.probability`
= the phone's `read_strip_evidence`. Add to `TextReaderParityTest`: on the parity pictures, the
set line's box within 2 px of the Python's (`foil_marker.set_line_box` on the reference's strip
lines, in `ocr_lines.json`) and the foil probability within 1e-2 of `FoilMarker.probability` on the
Python's crop. This closes the gap the README names ("finding the set line needs the server's OCR").

### Step 5 - Speed and memory on a phone (needs the user)

The emulator says nothing about speed (x86 host). The user's Pixel 10 runs the release build, and
the test build cannot be installed over it without wiping its data (app's CLAUDE.md) - **ask the
user** for the device and the moment: a second phone, or their go-ahead to reinstall. Then a
benchmark test (`OcrBenchmarkTest`, instrumented, on the 6 parity pictures, 10 reads each after a
warm-up) logs: time per whole card, per strip, peak memory (`Debug.getNativeHeapAllocatedSize`),
and the time of each forward. Write the numbers into the two READMEs (phone model, date).

**Decision point**: under ~1.0 s per card the dnn route stays. Above it, first try reading only
what the parsers need (detect on the whole card once; recognise only the lines in the title band
and the bottom-left region - the parsers ignore the rest), and only then the onnxruntime `Inference`
(step 0's fallback). An int8 quantisation of the two models is a later option; it changes the
answers and needs step 1's comparison again.

### Step 6 - Wiring into standalone mode, behind a setting (1 day)

As in "Where it fits on the phone". Also:
- the review item keeps what OCR read when the AI was asked (the server's suggestion for review);
- `ScanHistory` / the scan row says *OCR* as `ServerOutcome.statusLine` does for the server's reads;
- a failure anywhere in the OCR path falls through to the AI - a card is never lost to it (the
  server's rule in `_read_with_ocr`);
- a unit test of the decision (`OcrFirstPolicyTest`): confirmed -> added without the AI; not
  confirmed -> the AI; OCR failed -> the AI; the foil fallthrough order.
- A trial run on the emulator against a **test server is not relevant here** (standalone), but
  the inventory written must be the emulator's own - never import test CSVs or clear the user's.

### Step 7 - Docs

- `../mtg-scanner-android/README.md`: the file mapping table gets the new files against
  `card_ocr.py` / `foil_marker.py`; the "What was measured" table gets step 1's and step 5's
  numbers, and what was never tried (other phones; cards of older frames; a sleeve's glare);
  the licence section names PaddleOCR / light-ocr (Apache-2.0) for the models. The "How this
  project was built" statement stays.
- `../mtg-scanner-android/CLAUDE.md`: "Where Things Live" and a pitfall: *the OCR pipeline's
  constants are light-ocr's `normalized-config.json`, not the models' yml; a change to any of them
  or to the model files means running `ocr_reference.py` and `compare_with_light_ocr.py` again*.
- This repository: `docs/PROGRAM_DOCUMENTATION.md` "OCR first" gets one paragraph that the
  Android app carries the same reader (and the two scripts); `docs/FEATURES.md` a line (with
  what was never tried); `CLAUDE.md`'s Android paragraph names the new files to keep in step.

## Testing (summary)

| What | Where | Against | Tolerance |
|---|---|---|---|
| The pipeline's design | `compare_with_light_ocr.py` on 241 pictures | light-ocr (`server.mjs cpu`) | >= 98% identical `parse_magic`; differences explained |
| Grow / order / crop size / tensor width / CTC decode | JVM `TextMathTest`, `TextRecognitionTest` | `ocr_math_reference.json` from the Python reference | exact (1e-6 on floats) |
| The parsers | JVM `CardTextTest` | `ocr_parser_reference.json`: Python `card_ocr` / `foil_marker` on light-ocr's real lines | exact strings |
| Model files | emulator `TextReaderParityTest` | SHA-256 in the reference | exact |
| Detection + recognition on a picture | emulator `TextReaderParityTest`, 6 PNGs | the Python reference's lines (+ trace) | same texts; boxes 2 px; confidence 0.02 |
| The parsers on the phone's own lines | emulator, same test | Python parsers on light-ocr's lines | exact fields |
| Set line box + foil probability | emulator, same test | `foil_marker` on the reference's strip lines | 2 px; 1e-2 |
| Thread safety | emulator | two concurrent reads | no crash, same answers |
| Speed, memory | a phone (step 5, with the user) | - | recorded, not asserted |
| The OCR-first decision | JVM `OcrFirstPolicyTest` | the server's `_read_with_ocr` order | exact outcomes |

What is **not** tested automatically, and must be said in the docs: reading quality on a phone's
own camera pictures other than the 41 recorded ones; cards of older frames; non-English cards
beyond what the 241 pictures hold; the APK's size on the Play of a real user's phone.

## Risks and what to do about them

- **OpenCV 4.14 on Android refuses a model that 5.0 on the desktop ran** -> step 0 finds it; the
  fallback is onnxruntime-android behind `Inference`.
- **Float differences between OpenCV versions / ARM and x86** -> tolerances in step 3; the texts
  must stay identical. If a text differs on a parity picture, look at the argmax trace: a near-tie
  between two characters is a real tolerance case, anything else is a bug.
- **Too slow on a phone** -> step 5's decision point (regions first, then onnxruntime, then int8).
- **APK size +31 MB** -> accepted now; a model download like the card data's is the later option.
- **The parsers' regexes behave differently in Kotlin** (`\s`, Unicode classes, `À-ÿ`) ->
  `CardTextTest` on 30 real line sets catches it; keep `RegexOption` defaults (no IGNORE_CASE).
- **A new light-ocr bundle on the server** (another model tier, other thresholds) -> the server's
  `package.json` pins `^0.5.8`; the port is of bundle `ppocrv6-small-native-20260719.1`
  (`manifest.json`'s `bundleId`), which `ocr_reference.py` asserts before copying.

## Not in this plan

- The picture-first order and the image index on the phone (`Decide.kt` wired in): needs the index
  download (`GET /api/image_index/download`), the guards (`accept_printing`, `confirm_by_ocr`),
  and the coverage count - its own plan. This plan makes the strip read it needs.
- Reading the strip on the server with this port (the server keeps light-ocr).
- Other games than Magic (only Magic has a parser on the server too).
- Changing `server.mjs`, `card_ocr.py` or any server behaviour.

## Rules for the sessions that build it

- Read first: this file; `../mtg-scanner-android/CLAUDE.md` (all of it: the build commands, the
  emulator and the real phone rules, the parity-test pattern); in this repository `CLAUDE.md`'s
  paragraphs on the Android app and on the foil marker; docs/PROGRAM_DOCUMENTATION.md "OCR first"
  and "Foil and finish"; `core/card_ocr.py` and `core/foil_marker.py` in full (they are short).
- The Kotlin goes into `../mtg-scanner-android`; the two scripts and this plan into
  `scanner-server`. **Never edit anything under `../scanner`** (the standalone mirror).
- Every constant in the Kotlin is named after its Python / config name in a comment, and every
  block says which Python lines it is a port of (the user wants the code commented generously, to
  learn from it).
- Never read a model file from `node_modules` at run time or in a test: copy it with
  `ocr_reference.py`, check its SHA-256.
- The real phone: ask before installing anything or capturing; the emulator for everything else.
  A capture on the user's phone in client mode goes into their real server's scanned cards.
- Report what was measured with its numbers, and say plainly what was not run. A step whose
  check fails is not done; do not move to the next.
- Commit and push only when the user asks; the commit messages' attribution lines as the session
  says.
