"""Sol round 2 of #178: gene #172 and #173 only ever refuse more than main did (8aad087). And Sol round 1 of #189: gene
#186 and #187 only ever refuse more than #178 did (8f0ac36). And gene #190 only ever refuses more than #189 did
(7689069). And gene #198 only ever refuses more than #196 did (7e79220). And gene #201 only ever refuses more than
#200 and #204 did (50008b5).
Each validator, kept verbatim beside this test as a reference, must keep every note this one keeps: for the reviewed
repros, and for generated and fuzzed messages, with and without emoji, symbols, invisible characters and apostrophes
spelled other ways."""
import importlib.util
import itertools
import random
import re
from pathlib import Path

import pytest

from gene.fri_memory import personal_note


def reference(sha):
    path = Path(__file__).with_name(f'reference_fri_memory_{sha}.py')
    spec = importlib.util.spec_from_file_location(f'gene._fri_memory_{sha}', path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = 'gene'
    spec.loader.exec_module(module)
    return module.personal_note


MAIN = reference('8aad087')  # Before #178.
CURRENT = reference('8f0ac36')  # #178, before #189.
PREVIOUS = reference('7689069')  # #189, before #190.
LATEST = reference('7e79220')  # #196 (#190), before #198 and #199.
NEWEST = reference('50008b5')  # #200 (#198, #199) and #204 (#203), before #201.
BOUNDS = {'8aad087': MAIN, '8f0ac36': CURRENT, '7689069': PREVIOUS, '7e79220': LATEST, '50008b5': NEWEST}
CLAIMS = [('games', 'i like chess'), ('nickname', 'call me dan'), ('nickname', 'my nickname is dan'),
          ('music', 'my fav band is slowdive'), ('projects', 'im building a mod'), ('nickname', 'call me comet')]


def leaks(messages, bound=MAIN):
    """Each (category, evidence, text) this module keeps and the bound refuses."""
    return [(category, evidence, text) for category, evidence, text in messages
            if personal_note({'category': category, 'evidence': evidence}, text) is not None
            and bound({'category': category, 'evidence': evidence}, text) is None]


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
    # gene #186 and #187: continued conditions, long modifier runs, second person.
    'if i win\nand finish the match early\nplease call me comet', 'if i win, and finish early\nplease call me comet',
    'i lost\nand finished early\nplease call me comet', 'i like chess, or so ya still always really just say',
    'i like chess yall say', 'i like chess you guys say', "i like chess if y'all ask me", 'i like chess, makes yall think',
    "i like chess, makes y'all think", 'my writing is messy, i like chess', "i've recently been writing songs, i like chess",
    'writing is fun, i like chess', 'the swearing was wild, i like chess', 'my mom writing that, i like chess',
    'her writing says i like chess', 'my writing i like chess', 'i was recently told by jo, i like chess',
    # Sol round 1 of #189: an attribution past a noun or activity use, and an aside inside an attribution clause.
    'her writing makes it perfectly clear that i like chess', 'after writing in her diary that i like chess, jo laughs',
    'writing is how jo communicates that i like chess',
    "i like chess, or so the line makes yall think, those are jo's words",
    # gene #190: an emoji or an apostrophe spelled another way inside a report or a condition.
    'if i win\nand finish \U0001F602 early\nplease call me comet', 'i like chess, my mom \U0001F602 would say',
    'i like chess, or so you \U0001F602 always really just still say', 'i like chess, y\u2018all say',
    'i like chess, my mom <:kek:123456789012345678> would say', 'i like chess, my mom \u2764\uFE0F would say',
]


@pytest.mark.parametrize('sha', BOUNDS)
def test_no_reviewed_repro_is_kept_where_a_bound_refuses_it(sha):
    messages = [(category, evidence, text) for text in REVIEWED for category, evidence in CLAIMS if evidence in text]
    assert len(messages) == len(REVIEWED)
    assert not leaks(messages, BOUNDS[sha])


SUBJECTS = ['', 'i ', 'we ', 'my mom ', 'jo ', 'she ', 'they ', 'people ', 'you ', 'ya ', 'u ', 'fri ', 'bro ', 'May ',
            'Will ', 'So ', 'Just ', 'who ', 'it ', 'lol ', 'MY MOM ', 'everyone ', 'the boys ', 'yall ', "y'all ",
            'you guys ', 'my ', 'the ', 'a ', "i've ", 'her ']
MODIFIERS = ['', 'was ', 'just ', 'really ', 'lol ', 'keeps ', 'has been ', 'was just ', 'really '*5, "i'm ", 'am ',
             'still always really just ', 'lol lol lol lol ', 'recently been ', 'finally ', 'is ', 'was recently ']
VERBS = ['say', 'said', 'says', 'saying', 'told', 'tell', 'telling', 'quoted', 'quoting', 'quote', 'insist', 'insists',
         'swear', 'swearing', 'sworn', 'wrote', 'written', 'writing', 'texted', 'texting', 'text', 'asked', 'asking',
         'reckon', 'reckoning', 'claims', 'believe', 'thought', 'think', 'thinking', 'according to']
OBJECTS = ['', 'me ', 'jo ', 'to me ', 'that ', 'it ', 'it down ', 'earlier ', 'by my mom ', 'a song ', 'all day ']
SEPARATORS = [' ', ', ', ' lol ', ', honestly, ', ' and ', ' - ', ', so ', ' \U0001F602 ', '\n', ': ']
TAILS = ['', ', i think', ', ive written a song', ', or so i was told', ', you say', ', ya say', ' if you ask me',
         ', my parents say', ', who would think', ' whenever we play', ' if that makes sense', ', she has written',
         ', or so ya still always really just say', ' yall say', ' you guys say', ' if yall ask me', ' if you guys ask me',
         ', makes yall think', ", makes y'all think", ', my mom lol lol lol lol would say', ' u say', ' is fun',
         ' is messy']


def generated(count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        category, evidence = rng.choice(CLAIMS)
        lead = (rng.choice(SUBJECTS) + rng.choice(MODIFIERS) + rng.choice(VERBS) + ' ' + rng.choice(OBJECTS)).strip()
        if rng.random() < 0.2:
            lead = rng.choice(VERBS) + ' ' + rng.choice(SUBJECTS).strip()  # Inverted: "said jo".
        before = '' if rng.random() < 0.15 else lead + rng.choice(SEPARATORS)
        yield category, evidence, before + evidence + rng.choice(TAILS)


# Sol round 1 of #189: a report word used as a noun or an activity, then an attribution any number of words on, and
# an aside to the listener inside someone else's clause after the claim.
NOUN_LEADS = ['her writing', 'my writing', 'after writing', 'writing', 'the swearing', 'tired of texting',
              "i've recently been writing", 'texting', 'my texting', 'i finally finished writing', 'the writing']
NOUN_MIDDLES = ['', 'makes it', 'makes it perfectly clear', 'is how jo communicates', 'in her diary', 'all night',
                'is fun', 'was wild', 'shows', 'proves to everyone', 'is messy,', 'songs,']
NOUN_JOINS = [' that ', ', ', ' ', ' saying ', ' and ', ' so ', ': ']
NOUN_TAILS = ['', ', jo laughs', ", or so the line makes yall think, those are jo's words", ' makes yall think',
              ', makes you guys think', ' if yall ask me', ", those are jo's words", ", or so the note makes ya think",
              ', or so her post makes y\'all think, says jo', ' if you guys ask me lol']


def attributions(count, seed):
    rng = random.Random(seed)
    for _ in range(count):
        category, evidence = rng.choice(CLAIMS)
        lead = f'{rng.choice(NOUN_LEADS)} {rng.choice(NOUN_MIDDLES)}'.strip() + rng.choice(NOUN_JOINS)
        yield category, evidence, ('' if rng.random() < 0.1 else lead) + evidence + rng.choice(NOUN_TAILS)


WORDS = ['i', 'my', 'mom', 'jo', 'she', 'they', 'you', 'ya', 'we', 'said', 'say', 'says', 'told', 'quoted', 'writing',
         'written', 'texting', 'swearing', 'insists', 'was', 'just', 'really', 'lol', 'so', 'and', 'but', 'if', 'when',
         'whenever', 'win', 'play', 'please', 'then', "that's", 'ok', 'it', 'down', 'that', 'by', 'me', 'May', 'Will',
         'according', 'to', 'who', 'would', 'think', 'not', 'jk', 'or', 'yall', "y'all", 'u', 'guys', 'still', 'always',
         'the', 'is', 'fun', 'messy', 'recently', 'been', 'finally', "i've", 'ask', 'makes', 'finish', 'early', 'her',
         'after', 'clear', 'how', 'communicates', 'diary', 'line', 'words', 'those', 'are', "jo's", 'of', 'tired']
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


LINES = ['if i win', 'whenever we play', 'and finish the match early', 'or tie', 'and then', 'then', "if that's ok",
         'i lost', "if i win, i'll stream", 'and', 'or so', 'lol', 'and if i lose', 'until further notice', 'and you',
         'or stream, then', 'please']


def condition_lines(count, seed):
    """gene #186's shape: lines before a nickname that open, continue, cross or answer a condition."""
    rng = random.Random(seed)
    for _ in range(count):
        evidence = rng.choice(['call me dan', 'call me comet', 'my nickname is dan'])
        lines = [rng.choice(LINES) for _ in range(rng.randint(1, 4))]
        ending = rng.choice(['\n', '\n', ',\n', '\n\n', ' lol\n'])
        yield 'nickname', evidence, '\n'.join(lines) + ending + rng.choice(['', 'please ', 'pls ', 'and ']) + evidence


@pytest.mark.parametrize('sha', BOUNDS)
def test_generated_condition_lines_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(condition_lines(20000, 186))
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 2000  # Many kept.
    assert not leaks(messages, BOUNDS[sha])


@pytest.mark.parametrize('sha', BOUNDS)
def test_generated_reports_and_conditions_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(generated(20000, 172))
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000  # Many refused.
    assert not leaks(messages, BOUNDS[sha])


@pytest.mark.parametrize('sha', BOUNDS)
def test_generated_attributions_past_a_noun_or_activity_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(attributions(20000, 189))
    # Many kept, by #189 too (836), which refuses most of these attributions already.
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 500
    assert not leaks(messages, BOUNDS[sha])


@pytest.mark.parametrize('sha', BOUNDS)
def test_fuzzed_messages_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(fuzzed(20000, 173))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1000
    assert not leaks(messages, BOUNDS[sha])


SPRINKLES = ['\U0001F602', '\u2764\uFE0F', '\U0001F468\u200D\U0001F469\u200D\U0001F467', '<:kek:123456789012345678>',
             '<a:dance:98765432109876543>', '\U0001F44D\U0001F3FD', '1\uFE0F\u20E3', '\U0001F1FA\U0001F1F8', '\u2B50',
             # gene #198: older symbols, text faces.
             '\U000000A9\U0000FE0F', '\U00002049', '\U00002122', '\U00002194', ':)', ';P', 'xD', '<3']
CURLY = ['\u2018', '\u201B', '\u2032', '\uFF07', '\u00B4', '\u2019', '\U000002BC', '\U000002BB']
# gene #198: what may sit inside a word, invisible or not.
INSIDE = ['\U0000200B', '\U00002060', '\U000000AD', '\U0000200D', '\U0001F602']


def sprinkled(messages, seed):
    """gene #190's and #198's shapes: each message with emoji standing in for spaces or added beside them, invisible
    characters and emoji inside its words, and its apostrophes spelled other ways, so every reading is tried against
    every bound."""
    rng = random.Random(seed)
    for category, evidence, text in messages:
        at = text.find(evidence)
        head, tail = text[:at], text[at + len(evidence):]

        def scatter(part):
            part = re.sub(r"(?<=\w)'(?=\w)", lambda _: rng.choice(CURLY), part)
            part = re.sub(r"(?<=\w)(?=\w)", lambda _: rng.choice(INSIDE) if rng.random() < 0.03 else '', part)
            return re.sub(r' ', lambda _: rng.choice([' ', ' ', f' {rng.choice(SPRINKLES)} ', rng.choice(SPRINKLES)]),
                          part)
        yield category, evidence, scatter(head) + evidence + scatter(tail)


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_emoji_and_other_apostrophes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(sprinkled(itertools.chain(generated(8000, 190), condition_lines(8000, 1900),
                                              attributions(4000, 19000), fuzzed(8000, 190000)), 190))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 2000
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 2000
    assert not leaks(messages, BOUNDS[sha])


def test_the_bound_is_a_real_check():
    """A validator that keeps what a bound refuses fails it: neither reference is this module under another name."""
    assert MAIN is not personal_note and MAIN({'category': 'games', 'evidence': 'i like chess'},
                                              'i was saying earlier, i like chess') is None
    assert CURRENT is not personal_note and CURRENT is not MAIN
    assert CURRENT({'category': 'games', 'evidence': 'i like chess'}, 'my writing is messy, i like chess') is None
    assert MAIN({'category': 'games', 'evidence': 'i like chess'}, 'my writing is messy, i like chess') is not None
    assert PREVIOUS is not personal_note and PREVIOUS not in (MAIN, CURRENT)
    hidden = 'i like chess, my mom \U0001F602 would say'
    assert PREVIOUS({'category': 'games', 'evidence': 'i like chess'}, hidden) is not None
    assert personal_note({'category': 'games', 'evidence': 'i like chess'}, hidden) is None
    assert LATEST is not personal_note and LATEST not in (MAIN, CURRENT, PREVIOUS)
    for hidden in ('i like chess, my mom :) would say', 'i like chess, my mom would sa\U0001F602y',
                   'i like chess, my mom \U0000200B would say'):
        assert LATEST({'category': 'games', 'evidence': 'i like chess'}, hidden) is not None
        assert personal_note({'category': 'games', 'evidence': 'i like chess'}, hidden) is None
    assert NEWEST is not personal_note and NEWEST not in (MAIN, CURRENT, PREVIOUS, LATEST)
    for hidden in ('i like chess, my mom would sa1\uFE0F\u20E3y', 'i like chess y\u200B\u2018all say',
                   'i like chess, my mom:P) would say', 'i like chess, my mom:P] would say'):
        assert NEWEST({'category': 'games', 'evidence': 'i like chess'}, hidden) is not None
        assert personal_note({'category': 'games', 'evidence': 'i like chess'}, hidden) is None
    assert list(itertools.islice(generated(5, 1), 5)) == list(itertools.islice(generated(5, 1), 5))
