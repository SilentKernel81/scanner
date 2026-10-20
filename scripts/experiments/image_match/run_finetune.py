#!/usr/bin/env python3
"""
The fine-tune, unattended, one stage after the other (the bar it is judged by is fixed in docs/plans/IMAGE_MATCH_EXPERIMENT.md):

  1. finetune.py on the GPU (about 90 minutes for 8 epochs)
  2. export_onnx.py: the network as ONNX and its parity with OpenCV 4.14's dnn (what the phone has)
  3. embed.py --model ft on the GPU: the whole gallery and the evaluation pictures
  4. evaluate.py for the fine-tune, and again for the colour hash (so both carry the held-out split)
  5. strip.py (the strip and the no-AI table) for the fine-tune, and hard_rescore.py for the corrected hard cases (both embeddings)
  6. compare_ft.py: the bar, point by point

    nohup venv/bin/python -I run_finetune.py > out/run_finetune.log 2>&1 &
"""
import os
import sys
from pathlib import Path

os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'          # before run_all copies the environment
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_all import EXPERIMENT_PYTHON, HOME, REPO_PYTHON, run, status

GPU_PYTHON = str(HOME / 'venv-gpu' / 'bin' / 'python')

run('fine-tune (GPU)', GPU_PYTHON, 'finetune.py', '--epochs', '8')
run('export ft.onnx + OpenCV dnn parity', EXPERIMENT_PYTHON, 'export_onnx.py', '--model', 'ft')
run('embed ft (GPU)', GPU_PYTHON, 'embed.py', '--model', 'ft')
run('evaluate ft_card', EXPERIMENT_PYTHON, 'evaluate.py', 'ft_card', '--require-full-gallery')
run('evaluate phash_rgb_card (adds the held-out split)', EXPERIMENT_PYTHON, 'evaluate.py', 'phash_rgb_card')
run('strip + no-AI table ft_card', REPO_PYTHON, 'strip.py', 'ft_card')
run('hard cases, corrected labels: ft_card', REPO_PYTHON, 'hard_rescore.py', 'ft_card')
run('compare against the bar', EXPERIMENT_PYTHON, 'compare_ft.py')
status('ALL STAGES FINISHED (finetune)')
