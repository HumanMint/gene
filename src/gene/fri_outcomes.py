"""Typed tool evidence and truthful replies that do not depend on another model."""

from enum import Enum

from .privacy import clean


class Outcome(str, Enum):
    COMPLETED = 'completed'
    REJECTED = 'rejected'
    FAILED = 'failed'
    UNCERTAIN = 'uncertain'


def receipt(kind, value):
    """Keep existing evidence fields while making completion semantics explicit."""
    result = {**value, 'kind': kind}
    if kind == 'reminder_cancel' and result.get('state') not in (None, 'cancelled'):
        state = result['state']
        result['blocked'] = ("The reminder is already being sent; cancellation cannot undo that send."
                             if state == 'sending' else f'The reminder is already {state}; it was not cancelled.')
    if result.get('blocked'):
        outcome = Outcome.REJECTED
    elif result.get('uncertain'):
        outcome = Outcome.UNCERTAIN
    elif result.get('error'):
        outcome = Outcome.FAILED
    elif kind == 'gene_command' and not result.get('output'):
        outcome = Outcome.UNCERTAIN
        result['uncertain'] = 'Gene returned no confirmed result. Check the player before retrying.'
    else:
        outcome = Outcome.COMPLETED
    result['outcome'] = outcome.value
    return result


def fallback(receipts):
    """Never infer playback, publish a private read, or repeat a side effect."""
    earlier = [one(r) for r in receipts[:-1] if r['outcome'] == Outcome.COMPLETED
               and r['kind'] in ('work_save', 'reminder_cancel')]
    return '\n'.join([*earlier, one(receipts[-1])])[:1800]


def one(last):
    """Render only typed evidence, without the final model's proposed wording."""
    outcome = last['outcome']
    if outcome == Outcome.REJECTED:
        return 'That step was rejected: '+clean(str(last['blocked']), 1200)
    if outcome == Outcome.FAILED:
        return 'That check failed: '+clean(str(last['error']), 1200)
    if outcome == Outcome.UNCERTAIN:
        return ('Completion is uncertain. '+clean(str(last.get('uncertain', '')), 1000)+
                ' I have not retried it. Check its current status before sending another request.')
    kind = last['kind']
    if kind == 'gene_command':
        return 'Gene returned:\n'+clean(str(last['output']), 1600)
    if kind == 'work_save':
        return f"Saved your work at version {last.get('version', 'confirmed')} ({last.get('state', 'saved')})."
    if kind == 'reminder_cancel' and last.get('state') == 'cancelled':
        return 'Reminder cancelled.'
    if kind in ('web_search', 'web_refine', 'web_sources') and last.get('answer'):
        return clean(str(last['answer']), 1700)
    return 'That check completed, but I couldn’t finish explaining it. No action was repeated; ask for current details.'


def unrecorded(receipts, *, reason='I couldn’t save this chat record.'):
    """A failed privacy/revision write must not hide an already known side effect.

    Do not quote read results or controller details here: the failed write may be
    a concurrent privacy/access rejection, rather than a temporary outage.
    """
    lines = []
    for item in receipts:
        if item['outcome'] == Outcome.COMPLETED and item['kind'] == 'work_save':
            lines.append(f"Your work was saved at version {item['version']}.")
        elif item['outcome'] == Outcome.COMPLETED and item['kind'] == 'reminder_cancel':
            lines.append('The reminder was cancelled.')
    last = receipts[-1]
    if last['outcome'] == Outcome.COMPLETED:
        if last['kind'] == 'gene_command':
            lines.append('Gene returned a confirmed command result.')
        elif last['kind'] not in ('work_save', 'reminder_cancel'):
            lines.append('That check completed.')
    else:
        lines.append({'rejected': 'That step was rejected.', 'failed': 'That check failed.',
                      'uncertain': 'Completion is uncertain.'}[last['outcome']])
    lines.append(reason+' No action was repeated; check current status before repeating an action.')
    return '\n'.join(lines)[:1800]
