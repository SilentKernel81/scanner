# report.py
# Sums up the reading log (data/logs/reading.jsonl, core/reading/record.py) - started by scripts/reading_report.py, like printing_check.report for the shadow log.
# What it is for: the picture's thresholds and guards were set on 20 known-wrong labels and a few simulations; this is the same question asked of live scans -
# how many cards each step settled, how often each guard refused, and, from the review's picks, how often the picture's best match was right when a guard
# refused it (what each guard costs). And a short list of captures to look at by eye.

import json
from pathlib import Path


def _records(log_file):
    """The log's lines as dicts (unreadable lines skipped), the rotated .1 first so that the order is the time order"""
    log_file = Path(log_file)
    lines = []
    for path in (log_file.with_name(log_file.name + '.1'), log_file):
        if path.is_file():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    lines.append(json.loads(line))
                except ValueError:
                    pass
    return lines


def _count(values):
    """{value: how many}, most first"""
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _quantile(values, q):
    """The q-quantile of a list of numbers (nearest rank), or None for none"""
    values = sorted(v for v in values if isinstance(v, (int, float)))
    if not values:
        return None
    return values[min(len(values) - 1, int(q * len(values)))]


def report(log_file, show=15, out=print):
    """
    Print the summary of a reading log and return it as a dict ({'captures', 'by_mode', 'by_reader', 'by_outcome', 'refused', 'picks_by_rank', 'refused_then_picked',
    'seconds'}) - None when there is no log yet.
    """
    log_file = Path(log_file)
    records = _records(log_file)
    if not records:
        out(f"No {log_file.name} yet: nothing has been read since the reading log exists (it is written by the server, data/logs/)")
        return None
    captures = [r for r in records if not r.get('review')]
    picks = [r for r in records if r.get('review')]
    percent = lambda n, whole: f"{100 * n / whole:.1f}%" if whole else '-'
    out(f"{len(captures)} captures read ({captures[0]['time'] if captures else '-'} to {captures[-1]['time'] if captures else '-'}), {len(picks)} review picks")

    # --- the configurations they were read in ---
    configuration = lambda r: f"{r.get('mode')}, OCR {'on' if r.get('ocr_on') else 'off'}, AI {'on' if r.get('ai_on') else 'off'}, " \
                              f"picture alone {'on' if r.get('accept_image_alone') else 'off'}"
    by_mode = _count(configuration(r) for r in captures)
    out("\nBY CONFIGURATION")
    for name, n in by_mode.items():
        out(f"  {n:6d}  {name}")

    # --- what settled each card, and what became of it ---
    reader = lambda r: ((r.get('result') or {}).get('reader') or 'none')
    by_reader = _count(reader(r) for r in captures)
    out("\nREAD BY  (picture: the picture alone; picture+strip: the collector line named the printing; light-ocr: the full-card OCR; the AI's name; none: nothing)")
    for name, n in by_reader.items():
        out(f"  {n:6d}  {percent(n, len(captures)):>6}  {name}")
    by_outcome = _count(((r.get('result') or {}).get('outcome') or 'not routed') for r in captures)
    out("  what became of them: " + ', '.join(f"{n} {k}" for k, n in by_outcome.items()))

    # --- the guards ---
    refused = _count(r['refused'] for r in captures if r.get('refused'))
    out(f"\nREFUSED BY A GUARD - {sum(refused.values())} of the picture's decisions")
    for name, n in refused.items():
        out(f"  {n:6d}  {name}")
    unread = [r for r in captures if r.get('refused') == 'ocr_did_not_confirm']
    if unread:
        nothing = sum(1 for r in unread if not r.get('refused_ocr_name'))
        line = sum(1 for r in unread if r.get('refused_ocr_name') and r.get('refused_ocr_line'))
        out(f"  ocr_did_not_confirm: {nothing} with nothing read (the name unreadable, or the reader down), {len(unread) - nothing} with a name"
            f" ({line} of them with a whole collector line)")
        out(f"  ... and then answered by the AI: {sum(1 for r in unread if (r.get('ai') or {}).get('asked'))}")

    # --- the review's picks: was the picture right when a guard refused it? ---
    by_rank = _count(p.get('rank') for p in picks)
    out(f"\nREVIEW PICKS - {len(picks)}: " + ', '.join(f"{n} at rank {k}" if k is not None else f"{n} not among the candidates" for k, n in by_rank.items()))
    # join each pick to its capture's record by the capture's uid: which guard (if any) sent the card there, and was the picture's best match the one picked
    by_key = {r['capture_key']: r for r in captures if r.get('capture_key')}
    refused_then_picked = {}
    for p in picks:
        r = by_key.get(p.get('capture_key'))
        if not r:
            continue
        guard = r.get('refused') or '(no guard: the picture did not decide)'
        right = p.get('rank') == 0
        counts = refused_then_picked.setdefault(guard, {'best_was_right': 0, 'best_was_wrong': 0})
        counts['best_was_right' if right else 'best_was_wrong'] += 1
    if refused_then_picked:
        out("  joined to their captures - the cost of each guard (best_was_right: the guard sent a card to review the picture had right):")
        for guard, counts in refused_then_picked.items():
            out(f"    {guard}: {counts['best_was_right']} right, {counts['best_was_wrong']} wrong")

    # --- time per step ---
    seconds = {}
    for step in ('picture', 'ocr', 'ai', 'total'):
        values = [(r.get('seconds') or {}).get(step) for r in captures]
        values = [v for v in values if v is not None]
        if values:
            seconds[step] = {'n': len(values), 'median': _quantile(values, 0.5), 'p95': _quantile(values, 0.95)}
    out("\nSECONDS PER STEP (only the captures the step ran for)")
    for step, s in seconds.items():
        out(f"  {step:8s} {s['n']:6d} captures, median {s['median']:.3f}, 95th percentile {s['p95']:.3f}")

    # --- to look at by eye ---
    out(f"\nTO LOOK AT BY EYE (the latest {show} of each)")
    alone = [r for r in captures if reader(r) == 'picture' and ((r.get('decision') or {}).get('similarity') or 1) < 0.6]
    out(f"  picture-alone adds under 0.6 similarity: {len(alone)}")
    for r in alone[-show:]:
        out(f"    {r['time']} {r.get('capture')}: {(r.get('result') or {}).get('name')} [{(r.get('result') or {}).get('set')} {(r.get('result') or {}).get('number')}], "
            f"similarity {(r.get('decision') or {}).get('similarity')}")
    split = [r for r in captures if (r.get('decision') or {}).get('path') == 'strong' and (r.get('ocr') or {}).get('read')
             and (r.get('ocr') or {}).get('collector_number') and (r.get('strip') or {}).get('number')
             and str((r['ocr']['collector_number'])).lstrip('0') != str(r['strip']['number']).lstrip('0')]
    out(f"  strong decisions whose strip and full-card OCR read different numbers: {len(split)}")
    for r in split[-show:]:
        out(f"    {r['time']} {r.get('capture')}: strip {r['strip'].get('set')} {r['strip'].get('number')}, OCR {r['ocr'].get('set_code')} {r['ocr'].get('collector_number')}")
    weak = [r for r in captures if (r.get('ai') or {}).get('verdict') == 'weak']
    out(f"  AI answers the picture only weakly agrees with: {len(weak)}")
    for r in weak[-show:]:
        answer = (r.get('ai') or {}).get('answer') or {}
        out(f"    {r['time']} {r.get('capture')}: {answer.get('name')} [{answer.get('set_code')} {answer.get('collector_number')}]")

    return {'captures': len(captures), 'by_mode': by_mode, 'by_reader': by_reader, 'by_outcome': by_outcome, 'refused': refused,
            'picks_by_rank': by_rank, 'refused_then_picked': refused_then_picked, 'seconds': seconds}


def main(argv=None):
    """scripts/reading_report.py [--show N] [--log PATH]"""
    import argparse
    from card_scanner.config import Config
    parser = argparse.ArgumentParser(description="What the reading log says: how cards were read, which guards refused the picture, what the review picked")
    parser.add_argument('--show', type=int, default=15, help="how many captures of each kind to list for looking at by eye")
    parser.add_argument('--log', default=str(Config.DATA_DIR / 'logs' / 'reading.jsonl'), help="the log to read (default: the server's)")
    arguments = parser.parse_args(argv)
    report(arguments.log, arguments.show)
