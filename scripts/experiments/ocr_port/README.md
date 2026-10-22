# The OCR reader ported to the phone - step 1: the Python reference

`docs/plans/ANDROID_OCR_PLAN.md`, step 1. light-ocr (the server's OCR reader, `src/ocr/`) is a C++ core in a Node addon: no reference file can be made of
it. `ppocr_reference.py` is the same reading in Python on OpenCV - the same two PP-OCRv6 small networks, every constant read from the bundle's
`normalized-config.json` by its key - so that the Android port can be checked against CODE; `compare_with_light_ocr.py` checks the reference against
light-ocr itself, on the same pixels (light-ocr is sent PNGs: the server's `CardOcr` sends JPEGs of quality 95, other pixels).

## Measured (2026-10-21, this machine, light-ocr 0.5.x with bundle `ppocrv6-small-native-20260719.1`, provider cpu)

241 pictures: the owner's 41 Pixel captures (`~/.cache/mtg-image-match/truth/rescan_cards/`) and the first 200 station captures
(`~/.cache/mtg-image-match/truth/captures/`), each read whole and its bottom-left strip (`foil_marker.strip_of`):

| | Reference = light-ocr |
|---|---|
| `card_ocr.parse_magic` of the card (name, number, set, foil) | **240 of 241 (99.6%)** - the one difference a name's letter (`Gothmog` / `Gothmos`) |
| `parse_strip` + `foil_marker.symbol_read` of the strip | **236 of 241 (97.9%)** - 4 of the 5 the marker character (`GN2` / `GN2*`, `LTR =` / `LTR *`, `NOB-` / `NOB·`, `HOB·` / `HOB+`), 1 a set letter (`HOB` / `HOS`) |
| Lines found | the same lines on every picture (0 found by one reader only) |
| Line texts | 3,812 of 3,899 paired lines identical (97.8%); the rest differ by one character at a line's edge or a space |
| Boxes | on the strips, every line whose text differs has the SAME box (to the pixel): the differences are in the recognition, not the detection |
| Time per picture (card + strip, this CPU) | reference 0.37 s, light-ocr 0.68 s |

**Against the truth, on the 200 station captures with a known finish**, the star / dot of the strip's set line: reference 75 right, 1 wrong,
124 unread; light-ocr 76 right, 1 wrong, 123 unread (most unread: older cards print no marker). The two readers are equally good; the remaining
differences are not a weaker reader.

## What was found on the way (and is in the reference now)

- **The detector's input size is `runtimeDefaults.detection`** (the longer side at most 960, each side rounded UP to a multiple of 32), not the
  config's `sourceDetectionResize` (PaddleOCR's own: near full size, rounded half to even): on 8 Pixel captures 82 of 95 lines the same text
  against 76 of 92.
- **The box is grown with Clipper's integer offset, not as the rectangle grown by the distance** (which the plan assumed, "no Clipper needed"):
  82 of 95 lines -> **93 of 95** on the same 8 captures. Clipper works on integer points and approximates the round joins; the rectangle fitted
  around them (`mini_box`) differs by about a pixel, and a pixel at a line's start changes its first character. `ppocr_reference.clipper_offset_round`
  is Clipper 6.4.2's `ClipperOffset` written out for one closed polygon with round joins (no library on the phone): the same min-area rectangle as
  pyclipper for 3,000 of 3,000 random quads (the points themselves differ for 283: Clipper's final union merges some - the rectangle is all that is used).
- **Recognition is one line at a time** (`runtimeDefaults.recognitionBatchSize: 1`): batches of 8 padded to the widest line (the config's
  `recognition.batch`) matched 563 of 863 strip lines against 821.
- **The inference engine does not matter**: the networks on onnxruntime (as light-ocr runs them) instead of OpenCV dnn give the same answers on every
  strip. On the phone (step 0) OpenCV 4.14 could not import the recognizer and onnxruntime ran it; the app then moved to **OpenCV 5.0**, whose dnn loads
  it and gives onnxruntime's answers to 2.3e-5 of a probability on the Pixel - one engine for both networks, 23 MB less native library.
- **Tried and worse** (strip lines identical, of 865; the reference as written: 821): the crop's size rounded instead of truncated 821 (the same),
  a linear warp 765, the boxes not rounded back in the picture 582, the content width truncated 797 / rounded 811, the line resized with
  INTER_LINEAR_EXACT 764, AREA 473, CUBIC 627, normalised before resizing 714, warped straight to 48 px high 603. What is left is sub-pixel arithmetic
  inside light-ocr's C++ (its OpenCV build and resize) that Python does not reproduce; the plan's gate is the parses, not the pixels.

## Not done here

- Station captures beyond the first 200, other phones, sleeved cards, older frames: not compared.
- The plan's step-1 gate is 98% for both parses: the card's passes (99.6%), the strip's is 97.9% - the 5 differences are explained above, and
  against the truth the reference reads the marker as well as light-ocr. The Kotlin port (step 3) is checked against THIS reference exactly.

    venv/bin/python scripts/experiments/ocr_port/compare_with_light_ocr.py ~/.cache/mtg-image-match/truth/rescan_cards ~/.cache/mtg-image-match/truth/captures --limit 200 --out report.json
