#!/usr/bin/env python3
"""
Runs every stage of the experiment after the Scryfall download, unattended, one after the other.

    nohup ~/.cache/mtg-image-match/venv/bin/python -I scripts/experiments/image_match/run_all.py > ~/.cache/mtg-image-match/out/run_all.log 2>&1 &

  1. waits for fetch_scryfall.py to end, then runs it once more (a picture that failed is fetched again)
  2. embeds the gallery and the evaluation pictures: phash, MobileNetV3 (whole card, art crop, no colour
     normalisation) and DINOv2-S
  3. evaluate.py for each, index_bench.py for the two networks
  4. strip.py (the bottom-left strip with light-ocr, and the no-AI decision table) for each network - it runs in
     the repository's venv, which has what card_scanner needs

A stage that fails is logged and the next one runs. out/run_all.status says which stage is running / ran last.
No stage needs an answer from anyone: this is the point of having a driver.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
HOME = Path.home() / '.cache' / 'mtg-image-match'
OUT = HOME / 'out'
EXPERIMENT_PYTHON = str(HOME / 'venv' / 'bin' / 'python')
REPO_PYTHON = str(REPO / 'venv' / 'bin' / 'python')
ENV = {**os.environ, 'TORCH_HOME': str(HOME / 'torch')}


def status(text):
    print(time.strftime('%H:%M:%S'), text, flush=True)
    (OUT / 'run_all.status').write_text(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {text}\n')


def run(label, python, script, *args):
    status(f'RUNNING {label}')
    t0 = time.time()
    result = subprocess.run([python, '-I', str(HERE / script), *args], env=ENV, cwd=str(REPO if python == REPO_PYTHON else HOME))
    status(f'{"done" if result.returncode == 0 else "FAILED (exit " + str(result.returncode) + ")"} {label} in {time.time() - t0:.0f}s')
    return result.returncode == 0


def downloading():
    """Is fetch_scryfall.py running? (looks at /proc, so no other program is needed)"""
    for pid in os.listdir('/proc'):
        if pid.isdigit():
            try:
                if b'fetch_scryfall.py' in Path(f'/proc/{pid}/cmdline').read_bytes() and int(pid) != os.getpid():
                    return True
            except OSError:
                pass
    return False


def main():
    while downloading():
        status(f'waiting for the download: {sum(1 for _ in (HOME / "images").iterdir())} pictures')
        time.sleep(120)
    run('fetch again (retry failures)', EXPERIMENT_PYTHON, 'fetch_scryfall.py')

    configs = [('phash', []), ('mnv3', []), ('mnv3', ['--crop', 'art']), ('mnv3', ['--no-normalise']), ('dino', [])]
    for model, extra in configs:
        run(f'embed {model} {" ".join(extra)}', EXPERIMENT_PYTHON, 'embed.py', '--model', model, *extra)

    names = ['phash_card', 'mnv3_card', 'mnv3_art', 'mnv3_card_raw', 'dino_card']
    for name in names:
        run(f'evaluate {name}', EXPERIMENT_PYTHON, 'evaluate.py', name, '--require-full-gallery')
    for name in ('mnv3_card', 'dino_card'):
        run(f'index benchmark {name}', EXPERIMENT_PYTHON, 'index_bench.py', name)
    for name in ('mnv3_card', 'dino_card'):
        run(f'strip + no-AI table {name}', REPO_PYTHON, 'strip.py', name)
    status('ALL STAGES FINISHED')


if __name__ == '__main__':
    main()
