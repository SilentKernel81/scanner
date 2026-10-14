# Windows camera station: plan

Date: 2026-10-09

A plan for running the camera station (`station_client.py`) on a Windows PC with a USB webcam.
Nothing here is implemented. Nothing in it has been tried on Windows either: this project has
only ever run on Linux, and the statements about Windows below are what the code and OpenCV's
documentation suggest, to be checked in phase 0 before anything is built on them.

## What a station is, and what is Linux-only in it

A station finds each card in the camera's frames, captures it once and uploads the picture; the
server does the rest. It is four files - `station_client.py` (connection, uploads),
`scanner.py` (capture loop, auto-capture, focus), `object_detector.py` (outline detection),
`settings.py` / `config.py` - on plain Python with OpenCV, NumPy, python-socketio, requests and
PyYAML, all of which exist for Windows.

Everything about *deciding* (detection, stillness, new-card rules, uploads, the server's
commands) is portable as it is. What is tied to Linux is how the camera is opened and
controlled, and how the station is packaged:

| Where | Linux-only today | On Windows |
|---|---|---|
| `scanner.py: _initialize_usb_camera` | `cv2.VideoCapture(index, cv2.CAP_V4L2)` | Another OpenCV backend: DirectShow (`CAP_DSHOW`) or Media Foundation (`CAP_MSMF`) |
| `scanner.py: _run_v4l2_command` (19 uses) | Sharpness, zoom, autofocus on / off and `focus_absolute` are set by running `v4l2-ctl` | No `v4l2-ctl`. The same UVC controls are reachable through OpenCV properties on DirectShow (`CAP_PROP_FOCUS`, `CAP_PROP_AUTOFOCUS`, `CAP_PROP_ZOOM`, `CAP_PROP_SHARPNESS`) - to be confirmed for the camera in use |
| `scanner.py: _query_focus_range` | Parses `v4l2-ctl --list-ctrls` for the focus control's min / max | OpenCV does not report a control's range. Either read it through DirectShow's `IAMCameraControl::GetRange` (a small COM call), or configure it |
| `scanner.py: _usb_camera_present` | Checks `/dev/videoN` so a missing camera is noticed without OpenCV's error output | Already returns "present" off Linux; opening simply fails and the existing retry loop takes over |
| `config.py: usb_camera_index` | A number, or a `/dev/v4l/by-id/...` path that stays the same camera | Only a number, and DirectShow's numbering can change when devices are plugged in. A camera chosen by name would be the stable equivalent |
| Raw JPEG frames | With `CAP_PROP_CONVERT_RGB = 0` V4L2 hands over the camera's JPEG; live frames are decoded at half size, captures at full size (`raw_mjpeg`, `half_size_decode`) | Unknown whether a Windows backend hands over raw JPEG. The code already falls back to frames decoded by OpenCV - correct, but more CPU at 2560x1440 |
| `station_client.py: main` | Default station id from `/etc/host_hostname` (mounted in Docker), else the machine's name | Falls back to the machine's name already |
| Packaging | Docker image (target `client`), `deploy/docker-compose.client.yml` with `/dev` mounted | Docker Desktop cannot hand a USB webcam to a Linux container in any simple way, so the station runs natively: Python, or one `.exe` |

## Approach

**One station, two sets of camera controls.** Keep a single `CardScanner`. Move the handful of
camera-specific operations behind a small object it calls instead of `v4l2-ctl`:

```text
CameraControls
    open(index, width, height, fps) -> cv2.VideoCapture
    set_sharpness(value), set_zoom(value)
    focus_range() -> (min, max) or None
    set_autofocus(on), set_focus(position)
```

with `V4L2Controls` (today's behaviour, moved, not changed) and `DirectShowControls`, chosen by
`sys.platform`. The alternative - a separate Windows client - would copy the auto-capture and
focus logic, and those two copies would then have to be kept in step, as the Android port
already must be.

The Linux side must behave exactly as before: this is the code the Pi station runs. The
refactoring step is done and verified on the Pi first, before any Windows code is added.

## Phases

### 0. Find out what the camera does on Windows (a probe script, no changes to the app)

A standalone `scripts/probe_camera_windows.py` that the user runs on the Windows PC with the
webcam plugged in. It prints, for DirectShow and for Media Foundation:

- which camera indexes open, and how long opening takes;
- whether 2560x1440 MJPG at 20 FPS is accepted, and the frame rate actually measured;
- whether `CAP_PROP_CONVERT_RGB = 0` returns the raw JPEG;
- for focus, autofocus, zoom and sharpness: whether a set is accepted and read back, and
  whether the picture actually changes (a sharpness score at several focus positions);
- how long a focus change takes to show in the frames (0.4 s on Linux - the sweeps depend on it).

Everything after this depends on its output. If manual focus cannot be set at all, the station
still works with continuous autofocus (as it does for a camera without `focus_absolute`), but
without focus lock, sweeps and drift tracking - and that should be decided knowingly.

### 1. Separate the camera controls (Linux only, no behaviour change)

Introduce `CameraControls` / `V4L2Controls`, replace the `_run_v4l2_command` calls. Verify on
the Pi station: start-up log identical, focus sweep and lock work, a run of real captures.

### 2. DirectShow controls and opening

`DirectShowControls` from phase 0's findings: backend, MJPG, resolution, the control
properties, the focus range (queried or from `config.yaml`). A fake-camera test for the logic;
the real thing only on the Windows PC.

### 3. Choosing the camera

Windows numbers cameras in an order that can change. Add choosing by name
(`SCANNER_CAMERA_NAME` / `camera.name`: a part of the device's name, e.g. "PowerConf"), resolved
to an index at start-up, and list the cameras found when it does not match. Needs a way to
enumerate DirectShow devices with their names (a small dependency or a COM call - to be chosen
in phase 0).

### 4. Running it on Windows

- `requirements-client.txt` already fits; `opencv-python-headless` has Windows wheels.
- `scripts/install-station.ps1`: creates a venv, installs the requirements, writes a `.env`
  (`SCANNER_SERVER`, station id / name, camera) and a `start-station.cmd`.
- `station_client.py` reads `.env` itself when started outside Docker (today Docker Compose
  does that).
- Start with Windows: a Task Scheduler task at log-on (the camera belongs to the logged-on
  session; a Windows service would not see it reliably). Restart on failure.
- Stopping cleanly: Ctrl+C / closing the window must release the camera, as SIGINT does in Docker.
- Optional, later: a single `.exe` with PyInstaller, so Python need not be installed.

### 5. Re-measure what was measured on Linux

`CLAUDE.md` asks for this before thresholds change. On the Windows PC: camera noise with no
card (the stillness thresholds), CPU use of the capture loop (with and without raw JPEG), the
focus timing, and a drop test of some dozens of cards. Thresholds stay as they are unless the
measurements say otherwise; any that differ become per-platform values, not new defaults.

### 6. Documentation

../INSTALL.md (a Windows station section), README.md (the stations table), ../DOCKER_GUIDE.md (a line
saying Docker is not the way on Windows), ../PROGRAM_DOCUMENTATION.md (the controls split, what
was measured), ../TEST_CASES.md (Windows cases, marked tested / not tested), CLAUDE.md (the new
pitfalls). The documentation must say plainly which parts were run on Windows and by whom.

## Where each part is done

The Windows work is to be done by a Claude Code session in a Windows VM that has the USB camera
passed through to it (the user's intention, 2026-10-09), working on a clone of this repository.

| Part | Where |
|---|---|
| Phase 1 (separating the controls, no behaviour change) | The Linux development machine, verified on the Pi station - before the Windows session starts, so it begins from that code |
| Phase 0 (probe), 2, 3, 4, 5 | The Windows VM, with the camera |
| Phase 6 (documentation) | Whichever session did the work it describes |

What the Windows session needs to know, because nothing but the repository travels with it:

- Read `CLAUDE.md` and the *Camera and detection* part of ../PROGRAM_DOCUMENTATION.md first.
- The server is the one in use, with the real collection. A station under test must not feed it:
  either run a second server with its own data folder and port (CLAUDE.md, *Testing*), or
  connect to the real one with a station id of its own and auto-add off, and clear that
  station's scanned cards afterwards. Never clear, forget or import anything else there.
- Do not change the Linux behaviour: the Pi station runs the same files.
- Commit per phase; mark in the documentation what was run on Windows and what was not.

**A VM is not a PC.** A webcam passed through to a VM goes over a virtual USB controller: high
resolutions can fail or stutter (2560x1440 MJPG needs the bandwidth of a real USB 2 high-speed
link at least; an emulated USB 1.1 controller will not do it), and frame rate and CPU numbers
measured there say little about a real Windows PC. So: phase 0 also records what the VM's USB
controller is and the frame rate reached; what works in the VM is "works on Windows"; the
measurements of phase 5 (noise, CPU, focus timing, drop test) count only when repeated on the
hardware the station will really run on - or are marked as measured in a VM.

## Risks and open points

- **Manual focus may not be controllable** through OpenCV on Windows for this camera. Fallbacks:
  DirectShow's `IAMCameraControl` directly, or autofocus only.
- **CPU**: without raw JPEG every 2560x1440 frame is decoded in full. Fallbacks: a lower live
  resolution with full-size captures, or a lower frame rate.
- **Media Foundation vs. DirectShow**: MSMF can take many seconds to open a camera and does not
  expose the controls the same way; DirectShow is the likely choice, to be confirmed.
- **Windows camera privacy settings** can block desktop programs; the install notes need the
  setting, and the station's "no camera" message should hint at it.
- **Another program holding the camera** (Teams, the Camera app): same symptom as on Linux, a
  camera that opens but sends nothing.
- **Sleep and USB power saving** on a PC that is also used for other things.
- **USB passthrough to the VM** (see above): a camera that works at 640x480 but not at full
  resolution there is a limit of the VM, not of the station.

## Questions before starting

1. Which webcam goes into the VM? (The same Anker PowerConf C200 as on the Pi, or another - the
   focus logic was tuned on the C200.) And is the VM the place the station will finally run, or
   only where it is developed?
2. Is installing Python on that PC acceptable, or should it be a single `.exe` from the start?
3. Should it start by itself when someone logs on, or be started by hand?
4. Is focus lock / sweep needed there, or is continuous autofocus enough to begin with?

## Order of work and size

Phase 0 is an hour of writing plus one run on the Windows PC. Phase 1 is the only part that
touches what runs today, and is verified on the Pi. Phases 2-4 are the Windows work proper;
5 and 6 close it. Each phase is its own commit.
