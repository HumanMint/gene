"""Sol round 2 of #178: gene #172 and #173 only ever refuse more than main did (8aad087). And Sol round 1 of #189: gene
#186 and #187 only ever refuse more than #178 did (8f0ac36). And gene #190 only ever refuses more than #189 did
(7689069). And gene #198 only ever refuses more than #196 did (7e79220). And gene #201 only ever refuses more than
#200 and #204 did (50008b5). And gene #207 and #201's item 4 only ever refuse more than #205 did (b1d4b88). And gene
#212's items 1-3 only ever refuse more than #211 did (ba5a0a6, the same validator as 1d59371), with no exemption: its
item 4 stays open (Sol round 1 of #217). And gene #221's F1 and F2 only ever refuse more than #217 did (7914787),
with no exemption. And gene #226's items 2 and 3 only ever refuse more than #225 did (a5bdada), with no exemption.
And gene #231 only ever refuses more than #229 did (3bddd69), with no exemption. And gene #236 only ever refuses more
than #235 did (deb420d), with no exemption.
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
STACKED = reference('b1d4b88')  # #205 (#201), before #207.
ASIDED = reference('ba5a0a6')  # #211 (#207, #201 item 4), before #212.
HEDGED = reference('7914787')  # #217 (#212 items 1-3), before #221.
MODIFIED = reference('a5bdada')  # #225 (#221 F1, F2), before #226.
BRIDGED = reference('3bddd69')  # #229 (#226 items 2, 3), before #231.
REJOINED = reference('deb420d')  # #235 (#231 items 1, 3), before #236.
BOUNDS = {'8aad087': MAIN, '8f0ac36': CURRENT, '7689069': PREVIOUS, '7e79220': LATEST, '50008b5': NEWEST,
          'b1d4b88': STACKED, 'ba5a0a6': ASIDED, '7914787': HEDGED, 'a5bdada': MODIFIED, '3bddd69': BRIDGED,
          'deb420d': REJOINED}
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
    # gene #231: what only spaced refuses, and a line break inside the report itself.
    'have' + chr(0xA0) + 'to say ;- / i like chess', 'i like chess, my mom :- /\nwould\nsay',
    # gene #236: what only bridged refuses, a chained auxiliary, and a report verb outside the vocabulary.
    'must\nsay :-\n/ i like chess', 'i like chess, my mom would\nhave\nsaid', 'i like chess, my mom would announce',
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


# gene #207's shapes: obfuscations stacked in one place. What shows nothing inside a face or a keycap, keycaps inside a
# word and around its apostrophe, slash faces attached to a word, and (#201 item 4) faces before an aside of the
# member's.
STACKS = ['\U0000200B', '\U00002060', '\U0000FE0F', '\U0000200B\U0000FE0F', '']
ATTACHED_FACES = [':P]', ':P)', ':/', ':-/', ';/', '=/', ':D', ';P']
KEPT_FACES = [';P', ':P', ';)', ':D', ';-)']
ASIDES_AFTER = ['i guess', 'i swear', 'we all know', 'i mean', 'lol i guess', 'i think', 'i thought', 'yeah i guess']


def stacked(messages, seed):
    rng = random.Random(seed)

    def keycap():
        return (rng.choice('0123456789#*') + rng.choice(['', '\U0000FE0F']) + rng.choice(STACKS) + '\U000020E3')

    def attached():
        face = rng.choice(ATTACHED_FACES)
        return face[0] + rng.choice(STACKS) + face[1:] + ' '

    def stack(part):
        part = re.sub(r"(?<=\w)'(?=\w)", lambda _: rng.choice(
            ["'", f'{keycap()}\U000002BC{keycap()}', f'{keycap()}\U00002019', '\U000002BC']), part)
        part = re.sub(r"(?<=\w)(?=\w)", lambda _: keycap() if rng.random() < 0.04 else '', part)
        return re.sub(r'(?<=\w) ', lambda _: rng.choice(
            [' ', ' ', ' ', attached(), f' {rng.choice(KEPT_FACES)} {rng.choice(ASIDES_AFTER)} ']), part)

    for category, evidence, text in messages:
        at = text.find(evidence)
        yield category, evidence, stack(text[:at]) + evidence + stack(text[at + len(evidence):])


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_stacked_obfuscations_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(stacked(itertools.chain(generated(6000, 207), condition_lines(6000, 2070),
                                            fuzzed(6000, 20700)), 207))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
    assert not leaks(messages, BOUNDS[sha])


# gene #212's shapes: slash faces with a nose, a trailing bracket or a backslash, attached or alone, with a space inside;
# shorthand and annotation controls inside faces, keycaps and words; and faces before a hedge of the member's from a
# wider vocabulary, past a run of modifiers, with what its clause says after it: nothing, the rest of an interrupted
# clause, a condition, a report, taking it back, or what looks like a clause of its own (Sol round 1 of #217).
SLASH_FACES = [':^/', ':/)', ':\\', ':-\\', ';\\)', ":'/]", '=/(', ':/ )', ':~/', ':~\\', ';-/ ]']
HIDDEN = ['\U0001BCA0', '\U0001BCA1', '\U0001BCA2', '\U0001BCA3', chr(0xFFF9), chr(0xFFFA), chr(0xFFFB), '\U0000200B',
          '']
HEDGES = ['i believe', 'i really honestly guess', 'i reckon', "i'd say", 'i know', 'we all know', 'i guess', 'i think',
          'i do honestly think', 'lol i suppose', 'i absolutely believe', 'i strongly think', 'i certainly hope',
          'i hope']
HEDGE_RESTS = ['', 'would say', 'when i win', 'if i win', 'my mom says', 'my mom says so', "i'm kidding", 'jk',
               'my opponent is kidding', 'its fun', 'when the game starts', 'my friends are better',
               'my mom is saying those exact words', 'that is only if i win', "it's not really true",
               'you win when we play', 'my opponent is good']


def asided(messages, seed, faces=None, hedges=None, breaks=None, rests=None):
    """The messages with #212's shapes stacked around the evidence: SLASH_FACES and HEDGES, or `faces` and `hedges`;
    with `breaks`, what stands after a face, before its hedge or the rest of the clause, instead of a space; and with
    `rests`, a face followed by one of them directly too (gene #231). Without them, the same messages as before."""
    rng = random.Random(seed)
    faces, hedges = faces or SLASH_FACES, hedges or HEDGES

    def after():
        return rng.choice(breaks) if breaks else ' '

    def hidden():
        return rng.choice(HIDDEN)

    def stack(part):
        part = re.sub(r"(?<=\w)(?=\w)", lambda _: hidden() if rng.random() < 0.03 else '', part)
        part = re.sub(r"(?<=\w)(?=\w)", lambda _: (rng.choice('0123456789') + '\U0000FE0F' + hidden() + '\U000020E3')
                      if rng.random() < 0.02 else '', part)
        return re.sub(r'(?<=\w) ', lambda _: rng.choice(
            [' ', ' ', ' ', (lambda face: face[0] + hidden() + face[1:])(rng.choice(faces)) + after(),
             ' ' + rng.choice(faces) + after(),
             f' {rng.choice(KEPT_FACES)}{after()}{rng.choice(hedges)} {rng.choice(HEDGE_RESTS)} '.replace('  ', ' '),
             # Two runs in a row, where #211's reading and the wider one drop different faces (both are read).
             f' {rng.choice(KEPT_FACES)}{after()}{rng.choice(hedges)} {rng.choice(KEPT_FACES)} {rng.choice(hedges)} ',
             *([' ' + rng.choice(faces) + after() + rng.choice(rests) + ' '] if rests else [])]),
            part)

    for category, evidence, text in messages:
        at = text.find(evidence)
        yield category, evidence, stack(text[:at]) + evidence + stack(text[at + len(evidence):])


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_212s_shapes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(itertools.chain(generated(6000, 212), condition_lines(6000, 2120),
                                           fuzzed(6000, 21200)), 212))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
    assert not leaks(messages, BOUNDS[sha])


# gene #221's shapes: slash faces with horizontal whitespace around the nose or the slash (F2), and hedges past
# modifiers outside #217's list, any word ending in -ly among them (F1), with #217's beside them.
SPACED_FACES = [':- /', ': -/', ': - /', ': /', ':-\t/', ':- \\', ':- / )', '; - \\ ]', ':~ /', '=  /']
MODIFIED_HEDGES = ['i probably think', 'i definitely believe', 'i firmly believe', 'i seriously think', 'i prob think',
                   'i def believe', 'i really probably honestly think', 'we lowkey seriously know', "i'd probably say",
                   'i genuinely guess', 'i believe', 'i guess']


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_221s_shapes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(itertools.chain(generated(6000, 221), condition_lines(6000, 2210), fuzzed(6000, 22100)),
                           221, SPACED_FACES, MODIFIED_HEDGES))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
    assert not leaks(messages, BOUNDS[sha])


# gene #226's shapes: slash faces with a Unicode space separator inside (item 2), and a line break after a face,
# before its hedge or the rest of a report, or inside a slash face (item 3).
SEPARATED_FACES = [':-' + chr(0xA0) + '/', ':' + chr(0x2009) + '/', ':' + chr(0x202F) + '-' + chr(0x202F) + '/',
                   ':-/' + chr(0xA0) + ')', ':' + chr(0x3000) + '/', ':-\n/', ': -\n/ ]', ':- /', ':/']
BREAKS = ['\n', '\r\n', ' \n ', '\n\n', chr(0xA0), chr(0x2028), ' ']


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_226s_shapes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(itertools.chain(generated(6000, 226), condition_lines(6000, 2260), fuzzed(6000, 22600)),
                           226, SEPARATED_FACES, MODIFIED_HEDGES, BREAKS))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
    assert not leaks(messages, BOUNDS[sha])


# gene #231's shapes: a line break inside the report itself, between an auxiliary or a modifier and its verb, after a
# face (and its own break) or with none.
BROKEN_REPORTS = ['would\nsay', 'would\r\nsay', 'would\n\nsay', 'would really\nsay', 'would\nsay so', 'did\ntell me',
                  'would\n' + chr(0x2028) + 'say', 'could\nsay', 'would\nhonestly\nsay', 'would say', 'says']


# gene #236's shapes: a chained auxiliary or an unlisted modifier split across lines, and report verbs outside the
# vocabulary, on one line or split.
CHAINED_REPORTS = ['would\nhave\nsaid', 'would\noften\nsay', 'has been\nsaying', 'will\nhave\ntold me',
                   'might\nhave\nmentioned', 'would announce', 'announced', 'explained it', 'would\nexplain', 'declared',
                   'whispered', 'pointed out', 'is always\nexplaining', 'would say', 'would\nsometimes\nsay']


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_236s_shapes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(itertools.chain(generated(6000, 236), condition_lines(6000, 2360), fuzzed(6000, 23600)),
                           236, SEPARATED_FACES, MODIFIED_HEDGES, BREAKS, CHAINED_REPORTS))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
    assert not leaks(messages, BOUNDS[sha])


@pytest.mark.parametrize('sha', BOUNDS)
def test_messages_with_231s_shapes_are_never_kept_where_a_bound_refuses_them(sha):
    messages = list(asided(itertools.chain(generated(6000, 231), condition_lines(6000, 2310), fuzzed(6000, 23100)),
                           231, SEPARATED_FACES, MODIFIED_HEDGES, BREAKS, BROKEN_REPORTS))
    assert sum(personal_note({'category': c, 'evidence': e}, t) is not None for c, e, t in messages) > 1500
    assert sum(BOUNDS[sha]({'category': c, 'evidence': e}, t) is None for c, e, t in messages) > 5000
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
    assert STACKED is not personal_note and STACKED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST)
    for category, evidence, hidden in (
            ('games', 'i like chess', 'i like chess, my mom:\U0000200BP] would say'),
            ('games', 'i like chess', 'i like chess, my mom would sa1\U0000FE0F\U0000200B\U000020E3y'),
            ('games', 'i like chess', 'i like chess, my mom:/ would say'),
            ('nickname', 'call me dan', 'don1\U0000FE0F\U000020E3\U000002BC1\U0000FE0F\U000020E3t ever please call me dan'),
            ('nickname', 'call me dan', 'if i win ;P i guess\nplease call me dan')):
        assert STACKED({'category': category, 'evidence': evidence}, hidden) is not None
        assert personal_note({'category': category, 'evidence': evidence}, hidden) is None
    assert ASIDED is not personal_note and ASIDED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST, STACKED)
    for category, evidence, hidden in (
            ('games', 'i like chess', 'i like chess, my mom:^/ would say'),
            ('games', 'i like chess', 'i like chess, my mom:\\ would say'),
            ('nickname', 'call me dan', 'if i win ;P i believe\nplease call me dan'),
            ('games', 'i like chess', 'i like chess, my mom:\U0001BCA0P] would say')):
        assert ASIDED({'category': category, 'evidence': evidence}, hidden) is not None
        assert personal_note({'category': category, 'evidence': evidence}, hidden) is None
    for category, evidence, hidden in (  # Pre-existing in #211, found in Sol round 1 of #217 (B5-B7).
            ('games', 'i like chess', 'i like chess, my mom :-\\ would say'),
            ('games', 'i like chess', 'i like chess, my mom:/ ) would say'),
            ('games', 'i like chess', 'i like chess, my mom:~/ would say'),
            ('nickname', 'call me dan', 'if i win ;P i absolutely believe\nplease call me dan'),
            ('games', 'i like chess', 'i like chess, my mom would' + chr(0xFFF9) + 'say'),
            ('games', 'i like chess', 'i like chess, my mom would sa' + chr(0xFFFA) + 'y')):
        assert ASIDED({'category': category, 'evidence': evidence}, hidden) is not None
        assert personal_note({'category': category, 'evidence': evidence}, hidden) is None
    assert HEDGED is not personal_note and HEDGED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST, STACKED, ASIDED)
    for category, evidence, hidden in (  # Pre-existing in #217, found in Sol round 2 of #217 (gene #221 F1, F2).
            ('nickname', 'call me dan', 'if i win ;P i probably think\nplease call me dan'),
            ('nickname', 'call me dan', 'if i win ;P i definitely believe\nplease call me dan'),
            ('nickname', 'call me dan', 'if i win ;P i firmly believe\nplease call me dan'),
            ('nickname', 'call me dan', 'if i win ;P i seriously think\nplease call me dan'),
            ('games', 'i like chess', 'i like chess, my mom :- / would say'),
            ('games', 'i like chess', 'i like chess, my mom : / would say')):
        assert HEDGED({'category': category, 'evidence': evidence}, hidden) is not None
        assert personal_note({'category': category, 'evidence': evidence}, hidden) is None
    assert MODIFIED is not personal_note and MODIFIED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST, STACKED, ASIDED,
                                                              HEDGED)
    for category, evidence, hidden in (  # Pre-existing in #225, found in Sol round 1 of #225 (gene #226 items 2, 3).
            ('games', 'i like chess', 'i like chess, my mom :-' + chr(0xA0) + '/ would say'),
            ('games', 'i like chess', 'i like chess, my mom :' + chr(0x3000) + '/ would say'),
            ('nickname', 'call me dan', 'if i win ;P\ni probably think\nplease call me dan'),
            ('games', 'i like chess', 'i like chess, my mom :- /\nwould say'),
            ('games', 'i like chess', 'i like chess, my mom\n:- / would say'),
            ('games', 'i like chess', 'i like chess, my mom :-\n/ would say')):
        assert MODIFIED({'category': category, 'evidence': evidence}, hidden) is not None
        assert personal_note({'category': category, 'evidence': evidence}, hidden) is None
    assert BRIDGED is not personal_note and BRIDGED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST, STACKED, ASIDED,
                                                            HEDGED, MODIFIED)
    for hidden in ('i like chess, my mom :- /\nwould\nsay', 'i like chess, my mom :- /\r\nwould\r\nsay',  # gene #231.
                   'i like chess, my mom would\nsay', 'i like chess, my mom :- /\ndid\ntell me'):
        assert BRIDGED({'category': 'games', 'evidence': 'i like chess'}, hidden) is not None
        assert personal_note({'category': 'games', 'evidence': 'i like chess'}, hidden) is None
    assert REJOINED is not personal_note and REJOINED not in (MAIN, CURRENT, PREVIOUS, LATEST, NEWEST, STACKED, ASIDED,
                                                              HEDGED, MODIFIED, BRIDGED)
    for hidden in ('i like chess, my mom would\nhave\nsaid', 'i like chess, my mom would\noften\nsay',  # gene #236.
                   'i like chess, my mom would announce', 'i like chess, my mom explained'):
        assert REJOINED({'category': 'games', 'evidence': 'i like chess'}, hidden) is not None
        assert personal_note({'category': 'games', 'evidence': 'i like chess'}, hidden) is None
    assert list(itertools.islice(generated(5, 1), 5)) == list(itertools.islice(generated(5, 1), 5))


def test_every_name_mains_validator_has_keeps_its_value():
    """gene #226: a new reading's names never shadow one main's rules read (a regex string named CONTINUES once
    replaced the condition lines' continuation words, and kept what main refused). Every constant and pattern of main's
    module (deb420d, gene #236) is the same here; only functions differ."""
    import gene.fri_memory as current
    spec = importlib.util.spec_from_file_location('gene._fri_memory_names', Path(__file__).with_name(
        'reference_fri_memory_deb420d.py'))
    main = importlib.util.module_from_spec(spec)
    main.__package__ = 'gene'
    spec.loader.exec_module(main)
    for name, value in vars(main).items():
        if name.startswith('__') or callable(value) and not hasattr(value, 'pattern'):
            continue
        mine = getattr(current, name)
        assert (mine.pattern == value.pattern) if hasattr(value, 'pattern') else mine == value, name


def test_a_messages_readings_are_read_in_the_same_order_whatever_the_hash_seed():
    """Which reading refuses first decides how much is read (#211's linearity fix): their order is fixed in the code,
    never a set's, so it's the same in every process, whatever its hash seed."""
    import os
    import subprocess
    import sys
    script = ('import json, sys; from gene.fri_memory import readings_of; '
              'print(json.dumps(readings_of(sys.argv[1], sys.argv[2])))')
    text = 'if i win ;P i believe\nmy mom:\U0001BCA0P] would s\U0001F602ay i like chess :^/ ok'
    runs = {subprocess.run([sys.executable, '-c', script, text, 'i like chess'], capture_output=True, text=True,
                           check=True, env={**os.environ, 'PYTHONHASHSEED': seed}).stdout for seed in ('0', '1', '2', '3')}
    assert len(runs) == 1 and len(json_list := __import__('json').loads(runs.pop())) > 4
    assert len({tuple(reading) for reading in json_list}) == len(json_list)  # Each once.
