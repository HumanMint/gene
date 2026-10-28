"""Grade the note proposals in Fri's memory eval with the controller's own note rules. Offline, no model.

Fri's `python -m fri.fri_eval --suite memory` prints one JSON line per case. Lines with a `keep` field are
proposal cases: the fictional message, what the reply model proposed, and what should be kept. This keeps
them as a chat save would (its first three proposals, through personal_note) and checks the expectation:

    docker exec fri-runtime-1 python -m fri.fri_eval --suite memory > memory.jsonl
    docker exec -i gene-controller-1 python -m gene.fri_memory_eval < memory.jsonl

`keep` is null when nothing should be kept. Otherwise at least one note must be kept, and every kept note
must be in one of keep.categories and mention one of keep.about. Other lines pass through untouched.
"""

import json
import sys

from .fri_memory import personal_note


def grade(row, check=personal_note):
    proposed = row.get('proposed') or []
    kept = [note for candidate in proposed[:3] if (note := check(candidate, row['message']))]
    want = row['keep']
    if want is None:
        passed = not kept
    else:
        passed = bool(kept) and all(category in want['categories'] and
                                    any(word.casefold() in evidence.casefold() for word in want['about'])
                                    for category, evidence in kept)
    return {'case': row['case'], 'kept': [list(note) for note in kept],
            'passed': passed and row.get('status') == 'completed'}


def main(lines, out=sys.stdout):
    failed = []
    for line in lines:
        row = json.loads(line) if line.strip() else {}
        if 'keep' not in row:
            continue
        graded = grade(row)
        print(json.dumps(graded), file=out, flush=True)
        if not graded['passed']:
            failed.append(graded['case'])
    return failed


if __name__ == '__main__':
    if failed := main(sys.stdin):
        raise SystemExit('Memory proposals failed: '+', '.join(failed))
