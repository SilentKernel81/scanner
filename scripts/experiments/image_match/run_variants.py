#!/usr/bin/env python3
"""
Second batch, after the first results (the plain perceptual hash beat the pretrained networks): variants of the
hash, then the strip + no-AI table for each. Same unattended pattern as run_all.py; writes out/run_all.status.

    nohup venv/bin/python -I run_variants.py > out/run_variants.log 2>&1 &
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_all import EXPERIMENT_PYTHON, REPO_PYTHON, run, status

# (embed.py arguments, the folder name embed.py writes)
VARIANTS = [(['--model', 'phash32'], 'phash32_card'),
            (['--model', 'phash_rgb'], 'phash_rgb_card'),
            (['--model', 'phash', '--crop', 'art'], 'phash_art'),
            (['--model', 'phash', '--no-normalise'], 'phash_card_raw')]


def main():
    for args, name in VARIANTS:
        run(f'embed {name}', EXPERIMENT_PYTHON, 'embed.py', *args)
        run(f'evaluate {name}', EXPERIMENT_PYTHON, 'evaluate.py', name, '--require-full-gallery')
    status('ALL STAGES FINISHED (variants)')


if __name__ == '__main__':
    main()
