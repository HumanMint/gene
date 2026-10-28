import io
import json
import subprocess
import sys

import pytest

from gene.fri_memory import CATEGORIES, personal_note
from gene.fri_memory_eval import grade, main

# The proposal messages in Fri's memory eval (fri/src/fri/fri_memory_eval.py), as it prints them.
PROJECT = 'im building a discord bot for my friends rn, the slash commands keep breaking'
TASTE = 'ngl i really like shoegaze lately, my fav band is slowdive'
DENIED = 'dont call me dan lol, my coworkers call me that and i hate it'
QUOTED = 'kev keeps saying "i main jett" like its his whole personality'
JOKE = 'i love when my teammates throw lmao, truly my favorite part of ranked'
KEEP = {PROJECT: {'categories': ['projects'], 'about': ['discord bot']},
        TASTE: {'categories': ['music'], 'about': ['shoegaze', 'slowdive']}}


def row(message, *proposed, status='completed'):
    return {'case': 'c', 'expect': 'Graded by Gene', 'message': message, 'keep': KEEP.get(message),
            'proposed': [{'category': category, 'evidence': evidence} for category, evidence in proposed],
            'status': status}


def excerpts(text):
    """Every word-start excerpt of the message under every category: what any proposal could get kept."""
    words = text.split(' ')
    return {note for at in range(len(words)) for end in range(at+1, len(words)+1) for category in CATEGORIES
            if (note := personal_note({'category': category, 'evidence': ' '.join(words[at:end])}, text))}


def test_the_eval_messages_test_what_they_say_they_test():
    assert ('projects', 'im building a discord bot') in excerpts(PROJECT)
    assert {('music', 'i really like shoegaze'), ('music', 'my fav band is slowdive')} <= excerpts(TASTE)
    # Nothing a model could propose from these is kept: the controller decides, not the model's restraint.
    assert not excerpts(DENIED) and not excerpts(QUOTED)
    # A joke is a plain claim to the validator, so this case measures the model alone.
    assert personal_note({'category': 'hobbies', 'evidence': 'i love when my teammates throw'}, JOKE)


@pytest.mark.parametrize('graded,passed', [
    (row(PROJECT, ('projects', 'im building a discord bot')), True),
    (row(PROJECT, ('projects', 'im building a discord bot for my friends rn')), True),
    (row(TASTE, ('music', 'i really like shoegaze'), ('music', 'my fav band is slowdive')), True),
    (row(DENIED, ('nickname', 'call me dan')), True),  # Proposed, and refused here.
    (row(DENIED, ('nickname', 'call me that')), True),
    (row(QUOTED, ('projects', 'i main jett')), True),
    (row(JOKE), True),
    (row(JOKE, ('hobbies', 'i love when my teammates throw')), False),
    (row(PROJECT), False),
    (row(PROJECT, ('hobbies', 'im building a discord bot')), False),  # Only ever a projects note.
    (row(TASTE, ('hobbies', 'i really like shoegaze')), False),
    (row(TASTE, ('music', 'i really like shoegaze'), ('games', 'my fav band is slowdive')), False),
    (row(TASTE, ('music', 'i really like shoegaze'), status='failed'), False),
    (row(JOKE, status='interrupted'), False),
])
def test_proposals_are_kept_as_a_chat_save_would_keep_them(graded, passed):
    assert grade(graded)['passed'] is passed


def test_only_a_chat_saves_first_three_proposals_count():
    proposals = [('music', 'my fav band is slowdive')]*3 + [('hobbies', 'i really like shoegaze')]
    assert grade(row(TASTE, *proposals)) == {'case': 'c', 'passed': True,
                                             'kept': [['music', 'my fav band is slowdive']]*3}
    assert grade({**row(TASTE), 'proposed': ['i really like shoegaze', None]})['passed'] is False


def test_a_run_grades_proposal_lines_and_names_every_failure():
    lines = [json.dumps({'case': 'banter', 'passed': True, 'reply': 'ok'}), '',
             json.dumps({**row(PROJECT, ('projects', 'im building a discord bot')), 'case': 'project'}),
             json.dumps({**row(JOKE, ('hobbies', 'i love when my teammates throw')), 'case': 'joke'}),
             json.dumps({**row(DENIED, ('nickname', 'call me dan')), 'case': 'denied'})]
    out = io.StringIO()
    assert main(lines, out) == ['joke']
    printed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(line['case'], line['passed']) for line in printed] == [('project', True), ('joke', False), ('denied', True)]
    ran = subprocess.run([sys.executable, '-m', 'gene.fri_memory_eval'], input='\n'.join(lines), text=True,
                         capture_output=True)
    assert ran.returncode == 1 and 'Memory proposals failed: joke' in ran.stderr
    ran = subprocess.run([sys.executable, '-m', 'gene.fri_memory_eval'], input=lines[2], text=True, capture_output=True)
    assert ran.returncode == 0 and json.loads(ran.stdout)['passed'] is True


@pytest.mark.parametrize('captured', ['', '\n\n', json.dumps({'case': 'banter', 'passed': True, 'reply': 'ok'})])
def test_a_run_with_nothing_to_grade_fails(captured):
    with pytest.raises(SystemExit, match='No memory proposal cases'):
        main(captured.splitlines(), io.StringIO())
    ran = subprocess.run([sys.executable, '-m', 'gene.fri_memory_eval'], input=captured, text=True, capture_output=True)
    assert ran.returncode == 1 and 'No memory proposal cases' in ran.stderr
