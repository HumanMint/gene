"""Sol round 2 of #178: gene #172 and #173 only ever refuse more than main did. Main's validator as it was (8aad087),
kept verbatim beside this test as a reference, must keep every note this one keeps: for the reviewed repros, and for
generated and fuzzed messages."""
import importlib.util
import itertools
import random
from pathlib import Path

from gene.fri_memory import personal_note


def reference():
    path = Path(__file__).with_name('reference_fri_memory_8aad087.py')
    spec = importlib.util.spec_from_file_location('gene._fri_memory_8aad087', path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = 'gene'
    spec.loader.exec_module(module)
    return module.personal_note


MAIN = reference()
CLAIMS = [('games', 'i like chess'), ('nickname', 'call me dan'), ('nickname', 'my nickname is dan'),
          ('music', 'my fav band is slowdive'), ('projects', 'im building a mod')]


def leaks(messages):
    """Each (category, evidence, text) this module keeps and main's refuses."""
    return [(category, evidence, text) for category, evidence, text in messages
            if personal_note({'category': category, 'evidence': evidence}, text) is not None
            and MAIN({'category': category, 'evidence': evidence}, text) is None]


# Every leak the reviews found, and the shapes around them: none may be kept.
REVIEWED = [
    # Sol round 1: inverted, parenthetical, name-like and long-modifier reports; "you"; a condition on the line before.
    'said jo, i like chess', 'SAID JO, i like chess', 'said jo \U0001F602 i like chess', 'says kev, i like chess',
    'my mom, as usual, said that, i like chess', 'May said earlier, i like chess', 'Will said earlier, i like chess',
    'she really really really really really said earlier, i like chess', 'So just said jo, i like chess',
    'you really really really really really said earlier, i like chess', 'ya just said jo, i like chess',
    'fri really really really really really said earlier, i like chess', 'bro just said jo, i like chess',
    'i like chess, my friends write', 'i like chess, you say', 'whenever we play\nplease call me dan',
    # Sol round 2: first-person quotation, a passive past an adverb, "ya", conditions across courtesy lines.
    'i am quoting my mom here, i like chess', 'i like chess, i quoted my mom', 'let me quote my mom, i like chess',
    'a quote from my mom, i like chess', 'i was just told by my mom, i like chess',
    'i like chess, or so i was just told by jo', 'i like chess, i’m told by jo', 'i like chess, or so ya say',
    "if i win\nif that's ok\nplease call me dan", 'if i win, then\nplease call me dan',
    # The own clauses round 1 and 2 kept that main refuses.
    'i was saying earlier, i like chess', 'let me tell you, i like chess', 'believe me, i like chess',
    'just a thought but i like chess', 'i keep saying i like chess', 'i texted jo earlier, i like chess',
    'been thinking all day, i like chess', 'ask anyone, i like chess', 'i like chess, i told jo about it',
    'i like chess and i wrote a song', 'just told jo, i like chess', 'told my mom, i like chess',
]


def test_no_reviewed_repro_is_kept_where_main_refuses_it():
    messages = [(category, evidence, text) for text in REVIEWED for category, evidence in CLAIMS if evidence in text]
    assert len(messages) == len(REVIEWED)
    assert not leaks(messages)


SUBJECTS = ['', 'i ', 'we ', 'my mom ', 'jo ', 'she ', 'they ', 'people ', 'you ', 'ya ', 'u ', 'fri ', 'bro ', 'May ',
            'Will ', 'So ', 'Just ', 'who ', 'it ', 'lol ', 'MY MOM ', 'everyone ', 'the boys ']
MODIFIERS = ['', 'was ', 'just ', 'really ', 'lol ', 'keeps ', 'has been ', 'was just ', 'really '*5, "i'm ", 'am ']
VERBS = ['say', 'said', 'says', 'saying', 'told', 'tell', 'telling', 'quoted', 'quoting', 'quote', 'insist', 'insists',
         'swear', 'swearing', 'sworn', 'wrote', 'written', 'writing', 'texted', 'texting', 'text', 'asked', 'asking',
         'reckon', 'reckoning', 'claims', 'believe', 'thought', 'think', 'thinking', 'according to']
OBJECTS = ['', 'me ', 'jo ', 'to me ', 'that ', 'it ', 'it down ', 'earlier ', 'by my mom ', 'a song ', 'all day ']
SEPARATORS = [' ', ', ', ' lol ', ', honestly, ', ' and ', ' - ', ', so ', ' \U0001F602 ', '\n', ': ']
TAILS = ['', ', i think', ', ive written a song', ', or so i was told', ', you say', ', ya say', ' if you ask me',
         ', my parents say', ', who would think', ' whenever we play', ' if that makes sense', ', she has written']


def generated(count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        category, evidence = rng.choice(CLAIMS)
        lead = (rng.choice(SUBJECTS) + rng.choice(MODIFIERS) + rng.choice(VERBS) + ' ' + rng.choice(OBJECTS)).strip()
        if rng.random() < 0.2:
            lead = rng.choice(VERBS) + ' ' + rng.choice(SUBJECTS).strip()  # Inverted: "said jo".
        before = '' if rng.random() < 0.15 else lead + rng.choice(SEPARATORS)
        yield category, evidence, before + evidence + rng.choice(TAILS)


WORDS = ['i', 'my', 'mom', 'jo', 'she', 'they', 'you', 'ya', 'we', 'said', 'say', 'says', 'told', 'quoted', 'writing',
         'written', 'texting', 'swearing', 'insists', 'was', 'just', 'really', 'lol', 'so', 'and', 'but', 'if', 'when',
         'whenever', 'win', 'play', 'please', 'then', "that's", 'ok', 'it', 'down', 'that', 'by', 'me', 'May', 'Will',
         'according', 'to', 'who', 'would', 'think', 'not', 'jk']
MARKS = [' ', ' ', ' ', ', ', '\n', '. ', '? ', '! ', ' - ', ': ', ' (', ') ', ' "', '" ', ' \U0001F602 ']


def fuzzed(count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        category, evidence = rng.choice(CLAIMS)
        tokens = [rng.choice(WORDS) for _ in range(rng.randint(0, 9))]
        at = rng.randint(0, len(tokens))
        tokens.insert(at, evidence)
        text = ''.join(token + rng.choice(MARKS) for token in tokens).strip()
        if rng.random() < 0.1:
            text = text.upper().replace(evidence.upper(), evidence)
        yield category, evidence, text


def test_generated_reports_and_conditions_are_never_kept_where_main_refuses_them():
    messages = list(generated(20000, 172))
    assert sum(MAIN({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000  # Main refuses many.
    assert not leaks(messages)


def test_fuzzed_messages_are_never_kept_where_main_refuses_them():
    messages = list(fuzzed(20000, 173))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1000
    assert not leaks(messages)


def test_the_bound_is_a_real_check():
    """A validator that keeps what main refuses fails it: the reference isn't this module under another name."""
    assert MAIN is not personal_note and MAIN({'category': 'games', 'evidence': 'i like chess'},
                                              'i was saying earlier, i like chess') is None
    assert list(itertools.islice(generated(5, 1), 5)) == list(itertools.islice(generated(5, 1), 5))
