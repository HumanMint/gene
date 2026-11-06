"""gene #260: a report whose clause is on the next line keeps no note.

The report rules end a clause at a line break, and so did every reading but rejoined's runs of "say", so with evidence
`i like chess`, `i was told` + a line break + `that i like chess` kept its note: the claim read as a sentence of its own.
An added reading (`carried`) joins the space before a line that opens with "that", and reads the rest as the passive
reading does. Every other reading stays as it was, so it only ever refuses more than main: a claim with a line of its own
after it, opening with "that", is kept as before.
"""
import random

import pytest

from gene.fri_memory import personal_note
from gene.store import Store
from tests.test_fri_conversation import assert_linear
from tests.test_fri_memory_main_bound import (
    BOUNDS,
    BREAKS,
    CHAINED_REPORTS,
    CLAIMS,
    HEARD_RUNS,
    MODIFIED_HEDGES,
    PASSIVE_RUNS,
    PASSIVE_SUBJECTS,
    PASSIVE_VERBS,
    SEPARATED_FACES,
    asided,
    leaks,
    reference,
)

# Main now: #245 (#239), the same module at 117db20 and 07eefd8. This only ever refuses more than it.
BOUND = {**BOUNDS, '117db20': reference('117db20')}
NOTE = {'category': 'games', 'evidence': 'i like chess'}


@pytest.mark.parametrize('text', [
    'i was told\nthat i like chess',  # The issue.
    'i was told\n\nthat i like chess', 'i was told \n that i like chess', 'i was told' + chr(0x2028) + 'that i like chess',
    'i was told\n' + chr(0x200B) + 'that i like chess', 'i was told\nThat i like chess', 'i was told\r\nthat i like chess',
    'i was informed via email\nthat i like chess', 'she was reminded\nthat i like chess', 'i heard\nthat i like chess',
    "i've heard\nthat i like chess", 'i was told by my mom\nthat i like chess', 'we were assured\nthat i like chess',
    # Any report verb the report rules know, not just a passive one; and the other line breaks, which main refuses too.
    'jo thinks\nthat i like chess', 'apparently\nthat i like chess', 'my mom would say\nthat i like chess',
    'they say\nthat i like chess', 'my mom would tell you\nthat i like chess', 'i was told\rthat i like chess',
    'i was told' + chr(0x2029) + 'that i like chess', 'i was told\x0cthat i like chess', 'i was told\x85that i like chess',
    'gg\ni was told\nthat i like chess',
    # A report verb outside the vocabulary, as the one line reads it ("jo would mention that i like chess" keeps none).
    'jo would mention\nthat i like chess', 'jo would point out\nthat i like chess', 'jo would explain\nthat i like chess',
    'my mom keeps mentioning\nthat i like chess', 'jo would announce\nthat i like chess',
    'jo would\nmention\nthat i like chess', 'my mom has\nbeen\nmentioning\nthat i like chess',
])
def test_a_report_with_its_clause_on_the_next_line_keeps_no_note(text):
    assert personal_note(NOTE, text) is None


@pytest.mark.parametrize('text', [
    'i like chess\nthat game was fun', 'i like chess\nthat was a good match', 'i like chess\nthat one is mine',
    'i like chess. i was told\nthat we raid at nine', 'that i like chess is no secret', 'i like chess\n\nthat said, gg',
])
def test_a_claim_with_a_line_of_its_own_after_it_keeps_its_note(text):
    assert personal_note(NOTE, text) == ('games', 'i like chess')


@pytest.mark.parametrize('category, evidence, text', [
    # Evidence holding a verb the joined message reads as "say": read as "say" in both, or the two differ, and the note
    # was refused where main keeps it.
    ('hobbies', 'i like to mention it', 'i like to mention it\nthat way'),
    ('hobbies', 'i like to explain it', 'i like to explain it\nthat way'),
])
def test_evidence_holding_a_reworded_verb_is_read_as_the_message_is(category, evidence, text):
    assert personal_note({'category': category, 'evidence': evidence}, text) == (category, evidence)


@pytest.mark.parametrize('text', [
    # A chain of auxiliaries no other reading joins, broken over lines before a "that" line.
    'i like chess, jo were\nnever\ntell\nthat i heard i do', 'i like chess, jo were\nnever\ntell\r\nthat i heard i do',
])
def test_a_report_after_the_claim_with_its_auxiliaries_over_lines_keeps_no_note(text):
    assert personal_note(NOTE, text) is None


@pytest.mark.parametrize('text', ['i was told\nthat i like chess', 'i was informed via email\nthat i like chess'])
def test_they_keep_no_note_when_saved_or_after_a_restart(store, text):
    saved = store.save_chat('guild', 'public:1', 'one', 0, text, 'reply', 'model', 'high',
                            notes=[NOTE])
    assert not saved['first_memory']
    assert not Store(store.path).chat_context('guild', 'public:1', 'one')['personal_memory']['notes']


def line_broken_reports(count, seed):
    """Reports with their clause on the next line, before the claim or after it, and claims with a line of their own
    after them, any of them with a run of blank lines and spaces."""
    rng = random.Random(seed)
    for _ in range(count):
        category, evidence = rng.choice(CLAIMS)
        report = rng.choice([rng.choice(PASSIVE_SUBJECTS)+rng.choice(PASSIVE_RUNS)+rng.choice(PASSIVE_VERBS),
                             rng.choice(HEARD_RUNS), 'my mom said', 'she told me', 'i was told by jo'])
        gap = rng.choice(['\n', '\n\n', ' \n ', '\r\n', chr(0x2028), '\n' + chr(0x200B)])
        shape = rng.randrange(3)
        if shape == 0:
            text = report + gap + rng.choice(['that ', 'That ', 'that, honestly, ']) + evidence
        elif shape == 1:
            text = evidence + rng.choice(['. ', ', ', '\n']) + report + gap + 'that we raid at nine'
        else:
            text = evidence + gap + rng.choice(['that game was fun', 'that one is mine', 'that said, gg'])
        yield category, evidence, text


@pytest.mark.parametrize('sha', BOUND)
def test_line_broken_reports_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(line_broken_reports(4000, 260), 260, SEPARATED_FACES, MODIFIED_HEDGES, BREAKS,
                           CHAINED_REPORTS))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 300
    assert not leaks(messages, BOUND[sha])


def test_a_line_before_that_is_joined_once_whatever_runs_before_it():
    """A run of space is crossed once (the pattern starts only where one does), and a run of reports or of "that" lines
    reads in linear time."""
    for pad in ('\n', ' \n', 'was told\n', 'that\n', '\nthat ', 'i heard\n\n', 'was told\nthat ', '\rthat ', ' \r that ',
                chr(0x2028) + 'that ', chr(0x2029) + 'that ', '\x0bthat ', '\x0cthat ', '\x85that ', '\x1cthat ',
                'gg\rthat ', 'gg' + chr(0x2028) + 'that '):
        assert_linear(lambda text: personal_note(NOTE, text), lambda length: (pad*length)[:length-12] + 'i like chess')
        assert_linear(lambda text: personal_note(NOTE, text), lambda length: 'i like chess' + (pad*length)[:length-12])
