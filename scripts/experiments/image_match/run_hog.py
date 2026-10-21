#!/usr/bin/env python3
"""
Third batch: HOG descriptors (OpenCV's HOGDescriptor, cosine similarity) against the hashes, then the strip + no-AI table for
each HOG size. Same unattended pattern as run_all.py / run_variants.py.

    nohup venv/bin/python -I run_hog.py > out/run_hog.log 2>&1 &
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_all import EXPERIMENT_PYTHON, REPO_PYTHON, run, status

for model in ('hog_s', 'hog'):
    run(f'embed {model}', EXPERIMENT_PYTHON, 'embed.py', '--model', model)
    run(f'evaluate {model}', EXPERIMENT_PYTHON, 'evaluate.py', f'{model}_card', '--require-full-gallery')
status('ALL STAGES FINISHED (hog)')
