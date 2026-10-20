#!/usr/bin/env python3
"""
"No behaviour change", shown picture by picture and not by totals (equal totals can hide swapped answers): the PREVIOUS implementation of PrintingCheck (taken
from git: `git archive <commit> src config.yaml`, extracted to data/logs/, which git ignores) and the WORKING TREE run the lookup and the check on the same pictures,
each in a process of its own, and every record is compared - the path, the printing, the strip, the lead, the orientation, the comparison with the resolved card,
and the check's verdict and gap.

    venv/bin/python -I scripts/experiments/image_match/compare_lookup_versions.py [commit, default a84dcb9] [--easy 300]
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
commit = next((a for a in sys.argv[1:] if not a.startswith('--')), 'a84dcb9')
easy = sys.argv[sys.argv.index('--easy') + 1] if '--easy' in sys.argv else '300'

old = REPO / 'data' / 'logs' / f'old_{commit}'
if old.exists():
    subprocess.run(['rm', '-rf', str(old)], check=True)
old.mkdir(parents=True)
archive = subprocess.Popen(['git', 'archive', commit, 'src', 'config.yaml'], cwd=REPO, stdout=subprocess.PIPE)
subprocess.run(['tar', '-x', '-C', str(old)], stdin=archive.stdout, check=True)
archive.wait()
(old / 'src' / 'ocr' / 'node_modules').symlink_to(REPO / 'src' / 'ocr' / 'node_modules')     # the OCR reader's packages are not in git

environment = {**os.environ, 'MTG_SCANNER_HOME': str(REPO)}                                   # the real data folder for both: database, index
results = {}
for label, source in (('previous', old), ('working tree', REPO)):
    target = REPO / 'data' / 'logs' / f"lookup_{label.replace(' ', '_')}.jsonl"
    done = subprocess.run([sys.executable, '-I', str(HERE / 'lookup_snapshot.py'), '--src', str(source), '--out', str(target), '--easy', easy],
                          env=environment, capture_output=True, text=True)
    sources = [l for l in done.stderr.splitlines() if l.startswith('source of printing_check')]
    print(f'{label}: {sources[0] if sources else done.stderr[-300:]}')
    results[label] = [json.loads(line) for line in target.read_text().splitlines()]

before, after = results['previous'], results['working tree']
assert len(before) == len(after), (len(before), len(after))
different = [(b, a) for b, a in zip(before, after) if b != a]
paths = {}
for b in before:
    paths[b['lookup']['path']] = paths.get(b['lookup']['path'], 0) + 1
print(f'{len(before)} pictures; lookup paths in the previous code: {paths}')
print(f'identical records: {len(before) - len(different)} of {len(before)}; different: {len(different)}')
for b, a in different[:5]:
    print('  picture', b['picture'])
    for key in sorted(set(b['lookup']) | set(a['lookup'])):
        if b['lookup'].get(key) != a['lookup'].get(key):
            print(f"    lookup.{key}: {b['lookup'].get(key)}  ->  {a['lookup'].get(key)}")
    if b['check'] != a['check']:
        print(f"    check: {b['check']}  ->  {a['check']}")
sys.exit(1 if different else 0)
