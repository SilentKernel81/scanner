# Image matching instead of the vision AI - experiment report (DRAFT)

Status: **draft, 2026-10-20.** Research only: nothing here is integrated, and nothing in this file has been tried on a phone.
What was measured on the development machine is marked *measured*; what was never tested is marked *not tested*.
Scripts: `scripts/experiments/image_match/` (one driver per batch: `run_all.py`, `run_variants.py`, `run_hog.py`).
Data and results live outside the repository, in `~/.cache/mtg-image-match/` (Scryfall pictures, read-only copies of the
server's captures, every embedding and result file).

## The question

Can the vision AI be dropped from scanning? The AI today reads what the light-ocr reader cannot confirm (name, collector number,
set code, the ★/• foil marker). The idea: embed every Scryfall printing picture once, look the scanned card up by image, and
settle which *printing* it is by reading only the bottom-left strip (collector number, set code, foil marker).

## What the AI actually does today (measured on the server's 6,313 settled captures)

light-ocr alone confirmed 6,104 (97%); the AI read 206 (3%); 58 went to the review queue (0.9%). So the AI is a small
share of the work, and "no AI" means those ~3% plus the foil marker have to be solved some other way.

## Data and how it is scored

All copied read-only from the server on 2026-10-19 (nothing was written there).

| Set | Pictures | What the truth is |
|---|---|---|
| collection | 4,725 | the photo kept with each collection entry; truth = the entry's printing that the user accepted |
| scanned | 5,972 | what the server received; truth = the set + number the server settled (read by OCR or the AI) |
| frames | 576 | the whole camera frame behind a scanned capture, warped here with the station's own corners |

The gallery is the 112,765 printings in the server's card database (all downloaded from Scryfall, `normal` size, 100 ms between
request starts, cached outside git).

**The truth is not independent of what is being tested.** Every label was first produced by light-ocr or the AI. So "the strip
alone is right 97-98%" and "strip and image agree: 100% right" measure agreement with the same OCR engine, not with the cards.
Looking at the pictures behind the mistakes (below) shows both kinds of label error: the AI naming the wrong card, and
Secret Lair variants labelled with the wrong variant number. Numbers are therefore "against the stored labels".

## Results

### 1. Which descriptor finds the right printing (measured; percent; exact printing, nearest 1 / nearest 5 of 112,765)

| Descriptor | Per card | collection | scanned | frames |
|---|---|---|---|---|
| **Colour hash** (16x16 DCT of each RGB channel, 768 bits) | **96 B** | 91.5 / 99.8 | 91.1 / 98.2 | 89.2 / 95.8 |
| Grey hash (16x16 DCT, 256 bits) | 32 B | 89.7 / 99.5 | 87.4 / 96.6 | 87.7 / 94.1 |
| Grey hash 32x32 (1,024 bits) | 128 B | 91.2 / 99.0 | 87.0 / 94.7 | 83.3 / 89.9 |
| Grey hash, art crop only | 32 B | 81.6 / 98.8 | 82.2 / 96.9 | 70.3 / 85.4 |
| HOG (OpenCV HOGDescriptor, 64x96, 2,772 floats, cosine) | ~11 KB | 88.2 / 99.6 | 89.3 / 98.5 | 87.2 / 92.7 |
| HOG compact (32x48, 540 floats) | ~2 KB | 84.5 / 94.9 | 81.3 / 91.4 | 69.4 / 78.1 |
| Colour hash 50% + HOG 50% | ~11 KB | 92.0 / 99.9 | 92.4 / 98.8 | 91.0 / 96.4 |
| MobileNetV3-Large, ImageNet weights, whole card | 3.8 KB | 21.2 / 29.4 | 20.1 / 28.2 | 25.9 / 36.1 |
| MobileNetV3, art crop | 3.8 KB | 58.5 / 78.9 | 60.8 / 78.4 | 56.8 / 69.8 |
| DINOv2-S/14 (CLS token) | 1.5 KB | 15.5 / 25.4 | 15.2 / 25.0 | 19.1 / 27.3 |

- The pretrained networks fail here, not because of a bug: stored and recomputed vectors match exactly (cosine 1.0000), a gallery
  picture finds itself 99%, but a darkened + blurred copy of a Scryfall picture finds its original only 23% of the time. Global
  pooling throws away position, and every card shares one frame layout.
- Both networks do run in OpenCV 4.14 dnn (the version the Android app ships), with exact parity to torch: MobileNetV3 12 MB,
  10 ms per picture; DINOv2-S 87 MB, 122 ms (this laptop's CPU; DINOv2 needed its position embedding fixed to the input size
  to export).
- Same-art reprints: 5-9% of the hash's top-1 answers are another printing of the same card with the same illustration: the image
  cannot tell them apart, the strip has to.
- Orientation (measured, colour hash): a card upside down gets 0% right, and for 2.0% of them (210 of 10,697) the image alone
  would add the wrong card with a clear lead (the other 98% have no lead and would go to review). The remedy is to look the picture
  up in both orientations and keep the more similar one: on the upright pictures that flips 19 of 10,697 (0.18%) and top-1 stays
  91.3%, so it costs nothing. (An upside-down card is then found by the rotated lookup by construction; that direction was not measured
  separately.) The phone interface should do both lookups, or the warp must guarantee the card is upright.

### 2. Index size and speed (measured; this laptop, 8 cores, one query at a time)

- Colour hash, 112,765 printings: 10.8 MB (+ ids). Brute force over +-1 floats: 1.8 ms; a packed-bit popcount search was *not tested*
  (it would be faster still).
- For reference, the networks' vectors: brute force fp32 12-17 ms; fp16 keeps 99.7-99.85% of the top-1 answers but ran slower in
  numpy (no fast half-precision path); int8 keeps 83% (MobileNetV3, 960-d) / 96% (DINOv2, 384-d). hnswlib (M16-32, ef 32-128): 0.1-0.9 ms
  per query but only 83-97% top-1 agreement with brute force, and the saved file is *larger* than the raw vectors (190-464 MB for
  112k printings). At this gallery size HNSW buys little over brute force.
- Size of 100,000 printings: 9.6 MB (96-byte hash), 384 MB / 192 MB / 96 MB (960-d fp32 / fp16 / int8).

### 3. Reading only the bottom-left strip (measured; light-ocr on a crop, 46-85 ms)

Number and set read on 98-99% of pictures. Looking the printing up by set + number alone gives the exact printing for **97.0%
(collection) / 98.6% (scanned)**. So the strip does most of the work and the image's job is the rest.

### 4. No AI: when is a card added by itself? (measured; colour hash; against the stored labels)

Rule A2: the strip's printing and one of the image's five candidates agree on set AND number, and have the same art as the
image's best match -> add it. Otherwise: the image alone if its best match leads the second by 0.05 (optional) -> else review.

| | added | of those, right | to review |
|---|---|---|---|
| collection, A2 only | 96.7% | 100.0% (0 wrong) | 3.3% |
| collection, A2 + image alone if it leads | 98.9% | 99.9% (6 wrong) | 1.1% |
| scanned, A2 only | 96.7% | 100.0% (1 wrong) | 3.3% |
| scanned, A2 + image alone if it leads | 97.8% | 99.5% (28 wrong) | 2.2% |
| the 129 AI-read captures, A2 only | 59.7% | 98.7% | 40.3% |
| the 129 AI-read captures, A2 + image alone | 84.5% | 88.1% (13 wrong) | 15.5% |
| (fused hash + HOG) scanned, A2 + image alone | 98.7% | 99.6% (25 wrong) | 1.3% |
| (fused hash + HOG) collection, A2 + image alone | 99.0% | 99.9% (6 wrong) | 1.0% |

(The hard-case rows above are against the AI's own labels, which are wrong on some; see below for the corrected scores.)

Today the review queue holds 0.9% of captures and the AI reads another ~2%.

**Looking at the "wrong" ones** (contact sheets, my judgement from the pictures, not the physical cards):
- Hard cases: I judged all 31 of the 129 AI-read pictures whose label and image answer differ (`truth/hard_relabel.json`):
  14 the picture is the *image's* card and the AI's label is wrong ("Jennifer Walters" for "Too Evil to Stay Dead", "Fabled
  Passage" for "Dependable Quinjet", 5 Deadpool "Worn Powerstone" pictures whose printed line points at the base printing);
  5 the label is right and the image wrong; 11 the same card in another printing (the strip's job); 1 a third card.
  So the AI misread **at least 15 of 128 (12%)** of the cards it handled, and those sit in the collection under the wrong name
  (the collection should be checked: a strong disagreement between a stored label and the image is cheap to flag).
- **Against the corrected labels** the 128 kept hard cases score: image alone right card 96.1%, exact printing 87.5%; rule A2
  alone adds 60.2% (0 wrong), A2 + image alone adds 85.2% (0 wrong), 14.8% to review (against the AI's labels: 88.1% right, 13 wrong).
  Verdicts only where label and image disagreed; where they agree both are assumed right. The corrected truth is stored as
  FIXED printing ids (`truth/hard_truth.json`, `freeze_hard_truth.py`): "image" means the colour-hash baseline's first answer, the one
  the verdicts were made on. (A first version took the truth of an "image" verdict from the model being scored, so a wrong pick of a
  model under test would have counted as right; a Codex review found it before the fine-tune was scored.)
- Collection: the 8 errors of rule A are Secret Lair Deadpool ("IFIYW") variants, 6 with a label that looks wrong (the picture
  matches the image's answer) and 2 where the strip points at the base printing (Secret Lair cards print the original set and
  number). The art check (A2) removes the second kind.
- Secret Lair is 2.5% of the collection photos; apart from the IFIYW cards the image is right 98.1%. The IFIYW cards
  (15 photos, 10 printings; none in the scanned set, because the OCR cannot resolve their numbers): strip alone 0%, image top-1 66.7%,
  top-5 100%. They differ only in small stickers and need a person or a finer-grained model.

### 5. Foil without the AI (measured; 13 sets, all recent)

The strip's marker is read on 70% of foil and 86% of regular cards and was never wrong when read (99.9%). 96.4% of pictures show a
printing that exists in both finishes, so for 18.5% of all pictures the marker is the only evidence and it was not read.
A small CNN (about 100k parameters, 32x128 grey crop of the set line) trained on the set lines, tested on cards it never saw:

| Cards that need a foil answer | right |
|---|---|
| all test crops (1,971) | 99.2% |
| OCR left the marker unread (381) - the AI's cases today | 97.6% (collection 97.1%, scanned 98.1%) |
| whole answer: OCR marker if read, else the classifier at confidence 0.9 | answers 99.6%, right 99.5% |

**Implemented in the server (2026-10-20, in the commit after this report; not deployed):** `core/foil_marker.py` + `core/foil_net.onnx`
(the exact weights measured here), asked after the OCR's own marker and before the AI's foil check; documented in
PROGRAM_DOCUMENTATION.md ("Foil and finish"); `tests/test_foil_marker.py`. Not in the Android app.

A guess from the set alone gets 48.8% on the unread cards (a coin flip), so the classifier reads the marker, not the set. Caveats:
the stored finish for the unread cards came from the AI's foil check or the user; only 13 sets, all recent; older cards have no
marker at all; 381 unread test crops (+-2 points); never exported or timed on a phone.

### 6. The Deadpool Secret Lair rescan (measured; 15 pictures, one round, one camera; labels from the physical cards)

On 2026-10-20 the owner scanned their own three versions of five Deadpool cards (regular, foil, "confetti foil") in a fixed order
(captures #1037-#1051 of the phone), so every picture has a label that comes from the physical card, not from the AI.
On Scryfall (set `sld`) regular and foil are the same entry `IFIYW-n` (1-5) and the foil-only entries are `IFIYW-6` to `IFIYW-10`;
"confetti foil = entries 6-10" is an inference that fits all 15 results.
- What the pipeline decided at scan time: **0 of 15 right** (5 added as the base printing, e.g. "Lightning Strike [Game Night 2019, 42]",
  finish "Regular" even for the foil and confetti copies; 10 in the review queue).
- Colour hash, nearest Scryfall picture: **15 of 15** the right entry, with clear margins (e.g. 0.67 against 0.49 for the regular entry): the camera
  does see the confetti, so the three versions are separable by picture except regular against foil (the same entry).
- Finish: the OCR marker (read on 14 of 15) else the classifier: 15 of 15. The classifier on its own was wrong on two confetti copies (0.19, 0.00):
  it is a fallback for an unread marker, and confetti foil may confuse it.
- The strip reads the printing the card PRINTS (`2xm 80`, `gn2 42`, three with the number unread), not the Secret Lair entry: set + number
  lookup cannot find these cards; the image must pick the printing when the strip names one with other art (rule A2 does this).
- The 5 collection photos (of 15) that every descriptor missed earlier were mislabelled entries, not hard pictures: the collection held
  the plain foil copy of each card under the foil-only entry (quantity 2). On the owner's go-ahead (2026-10-20) one copy of each was moved
  to IFIYW-n foil through the app's own Edit-card route (`deadpool_fix.py`, after a backup `2026-10-20_09-24-15`); the collection now
  holds regular x1, foil x1, confetti foil x1 of each of the five cards.
- Not tested: a second round (placement variety), ordinary foil/regular pairs, other cameras.

## Provisional recommendation

**Build it - as a hash + strip + foil-crop pipeline, no AI call and no neural network for the lookup.** *Provisional only until the
printing check (shadow mode, built) has run on live scans; the fine-tune bar was tried and passed on thin evidence, and is not adopted
(see below).*
- Lookup: the 96-byte colour hash of the warped card against the printings (10.8 MB for 112,765), the five nearest as candidates.
- Printing: the strip read (number + set); accept it when it names one of the candidates with the same art as the best match.
- Foil: the marker when read, the classifier otherwise; a printing that exists in one finish needs neither.
- Unsure (about 1-3%): the review queue, as today, instead of the AI.
- It trades the AI's ~2% for review items, with no cloud or GPU dependence, and can be extended to a new set by hashing the
  set's pictures on the server (no retraining).

## On-phone format (proposal, not tested)

- `index.bin`: 112,765 x 96 bytes of hashes + a parallel list of printing ids (16-byte UUIDs or 4-byte row numbers into the card
  database the app already has), about 11-13 MB; downloaded after install (not bundled in the APK), updated by appending new
  printings from the server.
- `foil_net.onnx`: the foil-marker classifier, a few hundred KB; runs through OpenCV dnn (its layers are plain convolutions).
  *Export and parity check not done.*
- Interface the app would call (per capture): `lookup(card_rgb) -> [(printing_id, similarity)] x5`,
  `foil_marker(strip_rgb) -> (foil | regular, confidence)`; the strip OCR stays what the app already has.
  The hash itself (DCT) is a few lines on OpenCV (`cv::dct`) and must be tested for parity with the Python code.

## Fine-tuning a network (option 2c) - BAR FIXED BEFORE THE RUN

One training run (8 epochs, no sweep), written down on 2026-10-20 before it started. Setup: MobileNetV3-Large with a position-keeping
head (128-d), trained on the Scryfall pictures only (one clean and one camera-degraded view of each visual class, InfoNCE, batches of
~48 classes made of whole groups of one card's versions); held out of training: every printing of the Spider-Man set `spm` plus a random 5%
of the visual classes (a *new set* test). It must be compared with the **colour hash**, under rule A2 (+ image alone if it leads by
0.05), on the same pictures. It passes only if ALL of these hold:
1. a lower review rate than the hash by **at least 0.3 percentage points on both** the collection and the scanned set, with no more
   than 10% more wrong additions; **or** frame top-1 at least **3 points** higher;
2. on the held-out ("unseen") cards, top-1 not more than 1 point below the hash's top-1 on the same cards;
3. on the corrected hard cases (`hard_rescore.py`, against the frozen printing ids of `truth/hard_truth.json`): review rate not higher
   than the hash's (14.8%) and no wrong addition.
If any fails, the report says "not worth it" (a valid outcome: it costs a 12 MB model and ~10 ms a card on the phone, against a model-free
11 MB hash).

**Result (2026-10-20, one run of 8 epochs, 11,232 steps, 2 h 8 min on an RTX 3050): the bar as written is PASSED - on thin evidence.**
The network exports to a 13.1 MB ONNX that OpenCV 4.14's dnn runs with exact parity to torch (cosine 1.000000), 10 ms a picture on this CPU.

| | colour hash | fine-tune |
|---|---|---|
| collection photos (4,725): top-1 / top-5 / wrong card | 91.5 / 99.8 / 0.3% | 91.6 / 99.2 / 1.4% |
| scanned pictures (5,972): top-1 / top-5 / wrong card | 91.1 / 98.2 / 2.3% | 92.3 / 98.5 / 2.1% |
| frames (576, 158 printings): top-1 / top-5 | 89.2 / 95.8 | 93.1 / 95.7 |
| rule A2 + image alone, review rate: collection / scanned | 1.06% / 2.19% | 1.88% / 1.74% |
| wrong additions: collection / scanned | 6 / 28 | 9 / 33 |
| held-out "new set" cards (2,668): top-1 / top-5 | 90.6 / 99.3 | 90.6 / 99.3 |
| trained-on cards (8,605): top-1 | 91.4 | 92.5 |
| corrected hard cases (128): review rate / right card | 14.8% / 96.1% | 6.2% / 98.4% |

How it passed: point 1 holds only through its alternative clause (frame top-1 +3.9 points, over the 3 the bar asked for); the review rate
got WORSE on the collection photos (1.06% -> 1.88%, wrong additions 6 -> 9, wrong-card 0.3% -> 1.4%) and better by 0.45 on the scanned ones.
Point 2 holds because there is no loss on unseen cards - and no gain either: the held-out top-1 is identical (90.6%), so what it learned
did not carry over to cards it had not seen (it did a little on the ones it trained on, +1.1). Point 3 holds with a real gain (review
14.8% -> 6.2%, 0 wrong additions, the right card 96.1% -> 98.4%) on 128 pictures, i.e. a handful of cards. The frames set is small (576
pictures: a standard error of about 1.3 points on 3.9), and its "or" clause was written so that a gain there could count.
During training the quick check stayed level with the hash (67.5% against 68.2%) and did not predict this result.

**Assessment: not adopted for now.** What it buys is on the dim, glared pictures (the hard cases, the camera frames); on the 10,697 collection
and scanned pictures it is not better, and it is worse on the collection's wrong-card rate. It costs a 13 MB model and a runtime on every
station and phone, against a 96-byte hash with no model, and a new index (128 numbers a printing: 58 MB in float32, ~14 MB in int8) that
must be rebuilt with the model. If the shadow log (printing check) shows the hash failing on dim or glared captures, the fine-tune is the
refinement to try - perhaps only for the captures where the hash is unsure.

## Not tested / open

- The 5 collection photos of Deadpool cards that every descriptor missed should be checked by the owner against the physical copies (see 6).
- The verdicts on the hard cases are my reading of the pictures, not checks against the physical cards.
- Foil on older cards, and on sets other than the 13 in the data.
- Anything on a phone: speed, memory, the hash's parity with a Kotlin port.
- Real camera frames of cards at an angle beyond what the station's warp already straightens.
- Fusing HOG with the hash: +0.5-1.3 points of top-1 for 115x the storage (see table 1); not recommended.

## How this was built

By an AI assistant (Claude), directed by the user, like the rest of the project; the numbers above were produced by the scripts
in `scripts/experiments/image_match/`, and what was only inspected by eye is said so.
