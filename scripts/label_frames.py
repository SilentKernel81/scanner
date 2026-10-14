#!/usr/bin/env python3
"""
Label the camera frames kept for training a card detector (core/frames.py, data/frames/).

Each frame has the outline the detector found (`corners` in its note). This page shows the
frame with that outline and lets you drag the four corners onto the card's real edge, then
saves `<capture_id>.label.json` beside the pair:

    {"corners": [[x, y] x4: top left, top right, bottom right, bottom left, in the frame's pixels],
     "target": "card_edge", "status": "labelled" | "no_card" | "unsure", ...}

The label is what the card should be cut by: the CARD's own edge, not its sleeve's and not the
pile under it. Nothing else is written, and the frames and their notes are never changed.

    venv/bin/python scripts/label_frames.py [--frames data/frames] [--port 8765]

Local only (127.0.0.1) and standard library only - no server and no card_scanner import, so it
runs against a copy of the frames from anywhere. Keys on the page: 1-4 pick a corner, arrows
nudge it (shift: 5 px), S saves and goes on, N / P next / previous, R resets to the detector's
outline, U shows only the frames not labelled yet.
"""
import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# A frame's name is the capture id (core/frames.py) or station_number_time: nothing else is
# ever used as a path here
STEM = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
STATUSES = ('labelled', 'no_card', 'unsure')


def clockwise(points):
    """The four points as top left, top right, bottom right, bottom left (whatever order they came in)"""
    points = [[float(x), float(y)] for x, y in points]
    by_sum = sorted(points, key=lambda p: p[0] + p[1])           # smallest: top left, largest: bottom right
    top_left, bottom_right = by_sum[0], by_sum[-1]
    rest = [p for p in points if p is not top_left and p is not bottom_right]
    top_right, bottom_left = sorted(rest, key=lambda p: p[1] - p[0])  # y - x: smallest is top right
    return [top_left, top_right, bottom_right, bottom_left]


class Frames:
    """The folder of frames: their notes, their labels, and writing a label"""

    def __init__(self, folder):
        self.folder = Path(folder)

    def stems(self):
        """Frames that have both their picture and their note, oldest first"""
        found = []
        for note in self.folder.glob('*.json'):
            if note.name.endswith('.label.json') or not STEM.match(note.stem):
                continue
            if (self.folder / f'{note.stem}.jpg').is_file():
                found.append((note.stat().st_mtime, note.stem))
        return [stem for _, stem in sorted(found)]

    def read(self, name):
        try:
            return json.loads((self.folder / name).read_text())
        except (OSError, ValueError):
            return None

    def listing(self):
        items = []
        for stem in self.stems():
            note = self.read(f'{stem}.json') or {}
            label = self.read(f'{stem}.label.json')
            items.append({'stem': stem, 'station': note.get('station'), 'number': note.get('number'),
                          'received': note.get('received'), 'status': label.get('status') if label else None})
        return items

    def detail(self, stem):
        note = self.read(f'{stem}.json')
        if note is None:
            return None
        return {'stem': stem, 'note': note, 'label': self.read(f'{stem}.label.json')}

    def save(self, stem, body):
        """Write one label (whole or not at all). Returns the label, or raises ValueError"""
        note = self.read(f'{stem}.json')
        if note is None:
            raise ValueError('no such frame')
        status = body.get('status')
        if status not in STATUSES:
            raise ValueError('status')
        corners = None
        if status != 'no_card':
            raw = body.get('corners')
            if not (isinstance(raw, list) and len(raw) == 4
                    and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)
                            for p in raw)):
                raise ValueError('corners')
            width, height = note.get('frame_width'), note.get('frame_height')
            if width and height and any(not (-0.05 * width <= x <= 1.05 * width and -0.05 * height <= y <= 1.05 * height)
                                        for x, y in raw):
                raise ValueError('a corner is far outside the frame')
            corners = [[round(x, 1), round(y, 1)] for x, y in clockwise(raw)]
        label = {'corners': corners, 'status': status, 'target': 'card_edge',
                 'frame_width': note.get('frame_width'), 'frame_height': note.get('frame_height'),
                 'detector_corners': note.get('corners'), 'labelled': time.strftime('%Y-%m-%d %H:%M:%S')}
        path = self.folder / f'{stem}.label.json'
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(label, indent=1))
        os.replace(temporary, path)
        return label


PAGE = r'''<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Label frames</title>
<style>
  :root { color-scheme: light dark; --bg:#f5f5f4; --fg:#1c1917; --panel:#fff; --line:#d6d3d1; --accent:#16a34a; --det:#2563eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#1c1917; --fg:#f5f5f4; --panel:#292524; --line:#44403c; --accent:#4ade80; --det:#60a5fa; } }
  body { margin:0; font:14px system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; gap:12px; align-items:center; flex-wrap:wrap; padding:8px 16px; background:var(--panel); border-bottom:1px solid var(--line); }
  header b { font-size:15px; }
  button { font:inherit; padding:5px 12px; border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--fg); cursor:pointer; }
  button.primary { background:var(--accent); color:#000; border-color:var(--accent); font-weight:600; }
  main { display:flex; gap:16px; padding:12px 16px; align-items:flex-start; flex-wrap:wrap; }
  #stage { position:relative; line-height:0; border:1px solid var(--line); touch-action:none; }
  #side { width:260px; display:flex; flex-direction:column; gap:10px; }
  #loupe { width:240px; height:240px; border:1px solid var(--line); background:#000; }
  .key { opacity:.7; font-size:12px; line-height:1.5; }
  .pill { padding:1px 8px; border-radius:99px; border:1px solid var(--line); font-size:12px; }
  .legend i { display:inline-block; width:18px; height:0; border-top:3px solid; vertical-align:middle; margin-right:6px; }
  #list { max-height:220px; overflow:auto; font-size:12px; border:1px solid var(--line); border-radius:6px; background:var(--panel); }
  #list div { padding:2px 8px; cursor:pointer; display:flex; justify-content:space-between; }
  #list div.on { background:var(--accent); color:#000; }
</style>
<header>
  <b>Label frames</b>
  <span id="where"></span>
  <span id="progress" class="pill"></span>
  <label><input type="checkbox" id="onlyNew"> only unlabelled</label>
  <span style="flex:1"></span>
  <button id="prev">&larr; Prev (P)</button>
  <button id="next">Next (N) &rarr;</button>
</header>
<main>
  <div id="stage"><canvas id="c"></canvas></div>
  <div id="side">
    <div class="legend">
      <div><i style="border-color:var(--det)"></i>detector's outline</div>
      <div><i style="border-color:var(--accent)"></i>your label (drag the corners)</div>
    </div>
    <canvas id="loupe" width="240" height="240"></canvas>
    <div>
      <button class="primary" id="save">Save &amp; next (S)</button>
      <button id="reset">Reset (R)</button>
    </div>
    <div>
      <button id="nocard">No card here</button>
      <button id="unsure">Unsure</button>
    </div>
    <div class="key">The label is the <b>card's own edge</b> - not its sleeve, not the cards under it.<br>
      Keys: 1-4 pick a corner, arrows nudge (shift: 5 px), S save, N / P next / previous, R reset.</div>
    <div id="note" class="key"></div>
    <div id="list"></div>
  </div>
</main>
<script>
// State: the list of frames, which one is shown, and its label's four corners in FRAME pixels
let items = [], shown = [], index = 0, frame = null;     // frame: {stem, note, label}
let corners = null, detector = null, picked = 0, dragging = -1;
const image = new Image();
const canvas = document.getElementById('c'), ctx = canvas.getContext('2d');
const loupe = document.getElementById('loupe'), lctx = loupe.getContext('2d');
let scale = 1;                                            // canvas pixels per frame pixel

const $ = id => document.getElementById(id);

async function loadList(keepStem) {
  items = await (await fetch('/api/list')).json();
  applyFilter(keepStem);
}

function applyFilter(keepStem) {
  shown = $('onlyNew').checked ? items.filter(i => !i.status || i.stem === keepStem) : items;
  const at = shown.findIndex(i => i.stem === keepStem);
  index = at >= 0 ? at : Math.min(index, Math.max(0, shown.length - 1));
  const done = items.filter(i => i.status).length;
  $('progress').textContent = `${done} / ${items.length} labelled`;
  $('list').innerHTML = shown.map((it, k) =>
    `<div data-k="${k}" class="${k === index ? 'on' : ''}"><span>${it.station || ''} #${it.number ?? ''}</span><span>${it.status || ''}</span></div>`).join('');
  $('list').querySelectorAll('div').forEach(d => d.onclick = () => show(+d.dataset.k));
  if (shown.length) show(index); else $('where').textContent = 'nothing to show';
}

async function show(k) {
  index = Math.max(0, Math.min(k, shown.length - 1));
  const it = shown[index];
  frame = await (await fetch('/api/frame/' + it.stem)).json();
  const n = frame.note;
  detector = n.corners ? n.corners.map(p => p.slice()) : null;
  const start = frame.label && frame.label.corners ? frame.label.corners : detector;
  corners = start ? start.map(p => p.slice()) : defaultBox(n);
  picked = 0;
  $('where').textContent = `${n.station} #${n.number}  ${n.received || ''}  ${n.frame_width}x${n.frame_height}` +
    (frame.label ? `  [${frame.label.status}]` : '') + (detector ? '' : '  (no outline was found)');
  const nk = n.new_card || {};
  $('note').textContent = `trigger: ${nk.kind || '-'}  fixed area: ${n.fixed_area ? 'yes' : 'no'}  ` +
    `card cut: ${n.card_size ? n.card_size.join('x') : '-'}`;
  document.querySelectorAll('#list div').forEach((d, j) => d.classList.toggle('on', j === index));
  image.onload = () => { fit(); draw(); };
  image.src = `/frame/${it.stem}.jpg`;
}

// No outline was found: start from a card-shaped box in the middle of the frame
function defaultBox(n) {
  const w = n.frame_width, h = n.frame_height, bw = w * 0.5, bh = bw * 1.4;
  const x = (w - bw) / 2, y = (h - bh) / 2;
  return [[x, y], [x + bw, y], [x + bw, y + bh], [x, y + bh]];
}

function fit() {
  const maxH = window.innerHeight - 90, maxW = window.innerWidth - 320;
  scale = Math.min(maxW / image.naturalWidth, maxH / image.naturalHeight);
  canvas.width = Math.round(image.naturalWidth * scale);
  canvas.height = Math.round(image.naturalHeight * scale);
}

function outline(points, color, dash) {
  if (!points) return;
  ctx.setLineDash(dash ? [8, 6] : []);
  ctx.strokeStyle = color; ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => i ? ctx.lineTo(p[0] * scale, p[1] * scale) : ctx.moveTo(p[0] * scale, p[1] * scale));
  ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  const css = getComputedStyle(document.documentElement);
  outline(detector, css.getPropertyValue('--det').trim(), true);
  outline(corners, css.getPropertyValue('--accent').trim(), false);
  corners.forEach((p, i) => {
    ctx.fillStyle = i === picked ? '#facc15' : css.getPropertyValue('--accent').trim();
    ctx.beginPath(); ctx.arc(p[0] * scale, p[1] * scale, i === picked ? 7 : 5, 0, 7); ctx.fill();
    ctx.fillStyle = '#000'; ctx.font = '11px sans-serif';
    ctx.fillText(i + 1, p[0] * scale - 3, p[1] * scale + 4);
  });
  drawLoupe();
}

// A 6x magnifier around the picked corner: the edge has to be placed to a pixel or two
function drawLoupe() {
  const p = corners[picked], r = 40;                       // frame pixels either side
  lctx.imageSmoothingEnabled = false;
  lctx.fillStyle = '#000'; lctx.fillRect(0, 0, 240, 240);
  lctx.drawImage(image, p[0] - r, p[1] - r, 2 * r, 2 * r, 0, 0, 240, 240);
  lctx.strokeStyle = '#facc15'; lctx.lineWidth = 1;
  lctx.beginPath(); lctx.moveTo(120, 0); lctx.lineTo(120, 240); lctx.moveTo(0, 120); lctx.lineTo(240, 120); lctx.stroke();
}

function eventPoint(e) {
  const box = canvas.getBoundingClientRect();
  return [(e.clientX - box.left) / scale, (e.clientY - box.top) / scale];
}

canvas.addEventListener('pointerdown', e => {
  const [x, y] = eventPoint(e);
  let best = -1, bestD = 18 / scale;                       // within ~18 screen px of a corner
  corners.forEach((p, i) => { const d = Math.hypot(p[0] - x, p[1] - y); if (d < bestD) { best = i; bestD = d; } });
  if (best >= 0) { dragging = picked = best; canvas.setPointerCapture(e.pointerId); draw(); }
});
canvas.addEventListener('pointermove', e => {
  if (dragging < 0) return;
  const [x, y] = eventPoint(e);
  corners[dragging] = [Math.max(0, Math.min(image.naturalWidth, x)), Math.max(0, Math.min(image.naturalHeight, y))];
  draw();
});
canvas.addEventListener('pointerup', () => { dragging = -1; });

async function save(status) {
  const body = {status, corners: status === 'no_card' ? null : corners};
  const answer = await fetch('/api/label/' + frame.stem, {method: 'POST', body: JSON.stringify(body)});
  if (!answer.ok) { alert('Not saved: ' + await answer.text()); return; }
  const stem = frame.stem, next = shown[index + 1];
  await loadList(next ? next.stem : stem);
}

$('save').onclick = () => save('labelled');
$('nocard').onclick = () => save('no_card');
$('unsure').onclick = () => save('unsure');
$('reset').onclick = reset;
$('next').onclick = () => show(index + 1);
$('prev').onclick = () => show(index - 1);
$('onlyNew').onchange = () => applyFilter(frame && frame.stem);
function reset() { corners = (detector || defaultBox(frame.note)).map(p => p.slice()); draw(); }

document.addEventListener('keydown', e => {
  const k = e.key;
  if (k >= '1' && k <= '4') { picked = +k - 1; draw(); return; }
  const step = e.shiftKey ? 5 : 1, move = {ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step]}[k];
  if (move) { corners[picked][0] += move[0]; corners[picked][1] += move[1]; draw(); e.preventDefault(); return; }
  if (k === 's' || k === 'S') save('labelled');
  else if (k === 'n' || k === 'N') show(index + 1);
  else if (k === 'p' || k === 'P') show(index - 1);
  else if (k === 'r' || k === 'R') reset();
});
window.addEventListener('resize', () => { if (image.naturalWidth) { fit(); draw(); } });
loadList().catch(e => { $('where').textContent = 'Could not load the list: ' + e; });
</script>
'''


def make_handler(frames):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet: the terminal is for the one line at start
            pass

        def send(self, status, body, content_type):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def json(self, value, status=200):
            self.send(status, json.dumps(value), 'application/json')

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == '/':
                return self.send(200, PAGE, 'text/html; charset=utf-8')
            if path == '/api/list':
                return self.json(frames.listing())
            match = re.match(r'^/api/frame/([A-Za-z0-9_-]{1,80})$', path)
            if match:
                detail = frames.detail(match.group(1))
                return self.json(detail, 200) if detail else self.send(404, 'no such frame', 'text/plain')
            match = re.match(r'^/frame/([A-Za-z0-9_-]{1,80})\.jpg$', path)
            if match and (frames.folder / f'{match.group(1)}.jpg').is_file():
                return self.send(200, (frames.folder / f'{match.group(1)}.jpg').read_bytes(), 'image/jpeg')
            self.send(404, 'not found', 'text/plain')

        def do_POST(self):
            match = re.match(r'^/api/label/([A-Za-z0-9_-]{1,80})$', unquote(urlparse(self.path).path))
            if not match:
                return self.send(404, 'not found', 'text/plain')
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(min(length, 10_000)) or b'{}')
                self.json(frames.save(match.group(1), body))
            except (ValueError, TypeError) as e:
                self.send(400, str(e), 'text/plain')

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Label the outlines of the camera frames kept as training data")
    parser.add_argument('--frames', default='data/frames', help="the folder of <id>.jpg + <id>.json pairs")
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    folder = Path(args.frames)
    if not folder.is_dir():
        sys.exit(f"{folder} is not a folder - give the one that holds the .jpg / .json pairs (--frames)")
    frames = Frames(folder)
    total = len(frames.stems())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(frames))
    print(f"{total} frames in {folder} - open http://127.0.0.1:{args.port}  (Ctrl+C stops)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
