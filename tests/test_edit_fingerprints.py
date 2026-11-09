"""gene #150 and #246: an edit retires another member's saved reply only where the reply holds what the edit took out.

Member 20 says where they live, then edits it out. Member 30's saved reply, whose request carried the message
(`drew_on`), quoted the old words, and stayed usable: an edit withdrew only member 20's own exchange (gene #150 left the
others, since retiring every reply that carried a message breadcrumbs most of a busy channel after a typo fix). Now Fri
sends a fingerprint of what the edit took out (hashed, never words: see fri_edit_fingerprint), and a reply that carried
the message and quotes it keeps only a breadcrumb, as a deletion's does. An unrelated reply in the channel stays, and so
does one holding only what the edit kept. Where neither Fri nor the controller has the text from before the edit,
nothing more goes than on main.

Precision first (Sol round 2 of #261): a reply goes only where it holds a run of four words the edit took out, with a
rare word in it that isn't a calendar word, or a distinctive number (a phone number, a long code) that isn't a date, a
time or a timestamp. A reply retired by mistake is a saved answer lost; one missed is only as main leaves it.
"""
import pytest

from gene import fri_edit_fingerprint
from gene import store as controller
from gene.fri_edit_fingerprint import elements, fingerprint, holds, numbers, removed, salt
from gene.fri_protocol import contract
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import withdraw
from tests.test_withdrawal_sources import replies, turn, world  # noqa: F401 (the fixture)

BEFORE = 'my new address is 4217 juniper crescent, come by friday'
AFTER = 'come by friday'
QUOTE = 'they said their new address is 4217 juniper crescent'
PARAPHRASE = 'so you live on juniper crescent now?'
UNRELATED = 'the raid starts at nine, bring potions'
KEPT_ONLY = 'come by friday works for me'


def said(store, text=BEFORE, age=60):
    """Member 20's exchange with the message, as Fri saved it."""
    message = source(age=age, channel='51')
    store.save_chat('123', 'public:51', '20', 0, text, 'noted', 'model', 'high', source=message, provenance=[])
    return message['id']


def nearby(store, message, *texts, user='30'):
    """Member 30's saved replies, each request having carried the message."""
    for age, text in enumerate(texts):
        turn(store, f'question {age}', user=user, drew_on=[message], reply=text, age=30-age)


def edited(client, how, message, printed):
    """The edit's withdrawal, by the message's exchange or by its ID."""
    if how == 'by its exchange':
        return withdraw(client, message, fingerprint=printed)
    return withdraw_saved(client, message, fingerprints={message: printed})


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
def test_the_repro_a_reply_quoting_what_the_edit_took_out_keeps_only_a_breadcrumb(world, how):  # noqa: F811
    """The issue: member 30's reply quoting member 20's old words stayed usable after the edit, and reached their next
    request. Now it keeps a breadcrumb; their unrelated reply in the same channel stays."""
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    assert edited(client, how, message, fingerprint(BEFORE, AFTER, salt())).status_code == 200
    assert replies(store) == [controller.DRAWN, UNRELATED]
    assert '4217' not in str(store.chat_context('123', 'public:51', '30'))  # Their next request reads none of it.


def test_a_paraphrase_and_a_reply_holding_only_what_was_kept_stay(world):  # noqa: F811
    """Precision first: a paraphrase quotes no run of four words, so it stays, as on main."""
    client, store = world
    message = said(store)
    nearby(store, message, PARAPHRASE, KEPT_ONLY, UNRELATED)
    withdraw(client, message, fingerprint=fingerprint(BEFORE, AFTER, salt()))
    assert replies(store) == [PARAPHRASE, KEPT_ONLY, UNRELATED]


def test_the_controllers_own_copy_is_read_where_fri_had_no_text_from_before(world):  # noqa: F811
    """Fri had the message neither in view nor in Discord's cache: it sends all of what the message says now, and the
    controller works out what was taken out from its saved exchange."""
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    withdraw(client, message, fingerprint=fingerprint(None, AFTER, salt()))
    assert replies(store) == [controller.DRAWN, UNRELATED]


def test_an_earlier_message_of_a_burst_is_read_from_its_row(world):  # noqa: F811
    """The message was the first of a burst Fri answered at once: the burst's row is the controller's copy of it."""
    client, store = world
    first, last = source(age=61, channel='51'), source(age=60, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'anyway', 'noted', 'model', 'high', source=last, provenance=[],
                    burst=[{'source': first, 'text': BEFORE}, {'source': last, 'text': 'anyway'}])
    nearby(store, first['id'], QUOTE, UNRELATED)
    withdraw(client, first['id'], fingerprint=fingerprint(None, AFTER, salt()))
    assert replies(store) == [controller.DRAWN, UNRELATED]


def test_by_id_a_message_only_replies_carried_goes_by_what_fri_says(world):  # noqa: F811
    """Nothing saved holds the message itself, but Fri's view had it before the edit: replies holding what it took out
    go, though an edit by ID otherwise only reads."""
    client, store = world
    message = source(age=60, channel='51')['id']
    nearby(store, message, QUOTE, UNRELATED)
    withdraw_saved(client, message, fingerprints={message: fingerprint(BEFORE, AFTER, salt())})
    assert replies(store) == [controller.DRAWN, UNRELATED]


@pytest.mark.parametrize('sent', ['what it says now only', 'nothing'])
def test_with_no_text_from_before_anywhere_nothing_more_goes_than_on_main(world, sent):  # noqa: F811
    """Nothing saved holds the message, and Fri had no copy of it before the edit: the replies that carried it stay,
    as on main."""
    client, store = world
    message = source(age=60, channel='51')['id']
    nearby(store, message, QUOTE, UNRELATED)
    extra = {'fingerprints': {message: fingerprint(None, AFTER, salt())}} if sent != 'nothing' else {}
    assert withdraw_saved(client, message, **extra).json() == {'ok': True, 'removed': 0}
    assert replies(store) == [QUOTE, UNRELATED]


def test_a_reply_that_carried_a_retired_one_goes_too_where_it_repeats_it(world):  # noqa: F811
    """Fri's reply to member 30, retired, was in view when member 40 asked: their reply that repeats it goes too, and
    one that only carried it stays."""
    client, store = world
    message = said(store)
    retired_reply = str(int(message)+1)
    turn(store, 'where do they live', drew_on=[message], reply=QUOTE, reply_id=retired_reply, age=30)
    turn(store, 'say again', user='40', drew_on=[retired_reply], reply='again: '+QUOTE, age=20)
    turn(store, 'anything else', user='40', drew_on=[retired_reply], reply=UNRELATED, age=10)
    withdraw(client, message, fingerprint=fingerprint(BEFORE, AFTER, salt()))
    assert replies(store) == [controller.DRAWN] and replies(store, '40') == [controller.DRAWN, UNRELATED]


def test_a_deletion_still_takes_every_reply_that_carried_it(world):  # noqa: F811
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    withdraw(client, message, deleted=True, fingerprint=fingerprint(BEFORE, AFTER, salt()))
    assert replies(store) == [controller.DRAWN, controller.DRAWN]


def test_an_edit_without_a_fingerprint_is_as_on_main(world):  # noqa: F811
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    assert withdraw(client, message).json()['removed'] == 1
    assert replies(store) == [QUOTE, UNRELATED]


def test_the_controller_offers_it_and_bounds_what_it_takes(world):  # noqa: F811
    client, _ = world
    assert 'edit-fingerprints' in contract()['capabilities']
    print_ = fingerprint(BEFORE, AFTER, salt())
    for bad in ({**print_, 'salt': 'x'}, {**print_, 'removed': ['not hex']}, {**print_, 'kept': ['ab'*9]},
                {**print_, 'other': 1}, {**print_, 'removed': ['ab'*8]*3001}, {**print_, 'complete': 'yes'}):
        assert withdraw(client, '1', fingerprint=bad).status_code == 422
    assert client.post('/chat/turn/withdraw/saved', headers=headers('fri'), json={
        'guild_id': '123', 'message_ids': ['1'], 'fingerprints': {'2': print_}}).status_code == 422  # Not one sent.


# Made-up edits, each with what it says after, replies that quote what it took out (a run of four words with a rare word
# in it, or a distinctive number), and replies in the same channel about something else.
EDITS = [
    ('my new address is 4217 juniper crescent, come by friday', 'come by friday',
     ['they said their new address is 4217 juniper crescent']),
    ("ugh my landlord raised the rent to 2400 a month, i'm so done", "ugh rent went up, i'm so done",
     ['your landlord raised the rent to 2400? thats insane']),
    ('i think i failed my chemistry midterm, please dont tell my parents', 'i think the midterm went ok',
     ["you think you failed my chemistry midterm? i won't tell", 'i failed my chemistry midterm too lol']),
    ('my sister gabriella starts chemotherapy on monday', 'never mind',
     ['your sister gabriella starts chemotherapy? hope it goes ok']),
    ("i'm moving to denver next month for the new job at lockheed", "i'm moving next month",
     ['the new job at lockheed sounds great']),
    ('call me at 555 0199 after six', 'call me after six', ['ok calling 555 0199 after six', 'your number is 5550199 right',
                                                            'saved 555-0199']),
    ('the wifi password for the stream house is bluefalcon77', 'ask me for the wifi password',
     ['the stream house is bluefalcon77, got it']),
    ('my diagnosis was in 2024, still processing it', 'still processing it', ['your diagnosis was in 2024? thats a lot']),
    ('i leave for surgery at 12:30 on 2024-05-01', 'never mind', ['you leave for surgery at 12:30? good luck']),
    ('text me at (555) 010-9999 tonight', 'text me tonight', ['texting (555) 010-9999 now', 'is 5550109999 right']),
    # A possessive's "'s" is dropped, so it reads as the name alone.
    ("my sister gabriella's chemotherapy starts on monday", 'never mind', ['so gabriella chemotherapy starts on monday?']),
]
ELSEWHERE = [
    'the raid starts at nine, bring potions', 'lol same, rent is brutal everywhere', 'anyone up for ranked tonight',
    "i'm so done with this patch honestly", 'gg everyone, that was a good run', 'what time is the stream tomorrow',
    'my cat knocked over my coffee again', 'come by friday works for me', 'that midterm sounds rough, good luck',
    'next month is going to be busy', 'the new job sounds exciting', 'honestly i feel the same about mondays',
    'call me when you get home', 'the wifi is slow again', 'denver is cold this time of year',
    'my sister also plays this game', 'please dont tell anyone i lost', 'password resets are annoying',
    'is the chemistry lab open', 'lockheed has a booth at the expo', 'lets queue after six',
    'the olympics were held in 2024', 'the raid starts at 12:30', 'the patch drops 2024-05-01',
    'raid at 12:30 on 2024-05-01', 'season two ends may 5, 2024',
]


@pytest.mark.parametrize('before, after, quoting', EDITS, ids=[edit[1] for edit in EDITS])
def test_quotes_hold_what_was_taken_out_and_replies_about_something_else_dont(before, after, quoting):
    """A quoted run of four words with a rare word in it, or a distinctive number, is enough; nothing else is."""
    printed = fingerprint(before, after, salt())
    taken = removed(printed)
    assert [reply for reply in quoting if not holds(reply, printed['salt'], taken)] == []
    assert [reply for reply in ELSEWHERE if holds(reply, printed['salt'], taken)] == []


@pytest.mark.parametrize('before, after, reply', [
    ('i broke up with jordan last night', 'rough night', 'sorry about jordan'),  # One name.
    ('teh raid is at nine tonight', 'the raid is at nine tonight', 'teh raid lol'),  # A typo.
    ('the wifi password for the stream house is bluefalcon77', 'ask me for the wifi password', 'stream house tour later'),
    ('the house party music was loud', 'never mind', 'music at the house tonight?'),  # Two everyday words.
    # Precision first (Sol round 2): no pairs, no lone names or rare words, no sums of them.
    ('my sister gabriella starts chemotherapy on monday', 'never mind', "hope gabriella's chemotherapy goes okay"),
    ("i'm moving to denver next month for the new job at lockheed", "i'm moving next month",
     'denver sounds great, congrats on lockheed'),
    ('my discord password is hunter2 lol jk', 'jk', 'hunter2 is a terrible password'),
    ('my new address is 4217 juniper crescent', 'never mind', 'number 4217 right?'),  # A house number alone.
    ('my new address is 4217 juniper crescent', 'never mind', 'you live on juniper crescent'),  # Three words.
    ('the door code is 2468', 'never mind', 'the code 2468 worked'),  # Four digits.
    ('my pin is 707815', 'never mind', 'is it 707815'),  # Six.
])
def test_a_name_a_pair_a_short_number_or_three_words_are_not_enough(before, after, reply):
    printed = fingerprint(before, after, salt())
    assert not holds(reply, printed['salt'], removed(printed))


# Sol round 1, B1, and round 2, R2-B1 and R2-B2: a year, a time or a date, or a run of calendar words, retired an
# unrelated reply naming the same one. None is enough: a run needs a rare word that isn't a calendar word, and a date,
# a time or a timestamp is never a distinctive number, written as one group, as a date with a time, or over lines.
GENERIC = [
    ('my diagnosis was in 2024', 'the olympics were held in 2024'),  # Round 1's repros.
    ('i will leave at 12:30', 'the raid starts at 12:30'),
    ('my surgery is on 2024-05-01', 'the patch drops 2024-05-01'),
    ('my surgery is on 5/1/2024', 'the event is on 5/1/2024'),
    ('my surgery is on 01.05.24', 'the event is on 01.05.24'),
    ('my last day is may 5, 2024', 'the update ships may 5, 2024'),
    ('my shift ends at 1530', 'the stream starts at 1530'),
    ('my shift ends at 12:30:15', 'the timer says 12:30:15'),
    ('i grew up in the 1990s', 'music peaked in the 1990s'),
    ('i got married on 20240501', 'the build is from 20240501'),
    ('i was born in 1998 and graduated in 2020', 'between 1998 and 2020 nothing happened'),
    ('i leave at 12:30 on 2024-05-01', 'raid at 12:30 on 2024-05-01'),
    ('my surgery is on May 1, 2024', 'the patch is on May 1, 2024'),  # R2-B1.
    ('my surgery is on friday 2024-05-01', 'the patch is on friday 2024-05-01'),
    ('my surgery is on friday, May 1st', 'the patch is on friday, May 1st'),
    ('my surgery is on Wednesday the first of May', 'the patch is on Wednesday the first of May'),
    ('my surgery is on 1 September 2024', 'the patch is on 1 September 2024'),
    ('my surgery is on 2024-05-01 12:30:15', 'the patch is on 2024-05-01 12:30:15'),  # R2-B2.
    ('my surgery is on 2024-05-01\n12:30:15', 'the patch is on 2024-05-01\n12:30:15'),
    ('my surgery is on 2024-05-01T12:30:15Z', 'the patch is on 2024-05-01T12:30:15Z'),
    ('my surgery is at 1714521600', 'the patch is at 1714521600'),  # A Unix time.
    ('my surgery is on 20240501123015', 'the patch is on 20240501123015'),
    ('my surgery is on 01.05.2024 at 12:30', 'the patch is on 01.05.2024 at 12:30'),
]


@pytest.mark.parametrize('before, unrelated', GENERIC, ids=[edit[1] for edit in GENERIC])
def test_a_year_a_time_a_date_or_a_run_of_calendar_words_is_not_enough(before, unrelated):
    printed = fingerprint(before, 'never mind', salt())
    assert not holds(unrelated, printed['salt'], removed(printed))


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
@pytest.mark.parametrize('before, unrelated', [GENERIC[12], GENERIC[17], GENERIC[18]], ids=['R2-B1', 'R2-B2', 'over lines'])
def test_the_r2_repros_retire_no_reply_about_another_date(world, how, before, unrelated):  # noqa: F811
    client, store = world
    message = said(store, before) if how == 'by its exchange' else source(age=60, channel='51')['id']
    nearby(store, message, unrelated)
    edited(client, how, message, fingerprint(before, 'never mind', salt()))
    assert replies(store) == [unrelated]


@pytest.mark.parametrize('written, distinctive', [
    ('5550199', True), ('555-0199', True), ('555 0199', True), ('555.0199', True), ('(555) 010-9999', True),
    ('+1 555 010 9999', True), ('+44 20 7946 0958', True), ('2125550199', True), ('70815421', True),
    ('4217', False), ('707815', False), ('12 34 56 78', False), ('1,234,567', False), ('2024 2025 2026', False),
    ('20240501', False), ('2024-05-01', False), ('01.05.2024', False), ('05-01-2024', False), ('5/1/2024', False),
    ('2024-05-01 12:30:15', False), ('2024-05-01T12:30:15', False), ('20240501123015', False), ('202405011230', False),
    ('1714521600', False), ('1714521600000', False), ('12:30:15', False), ('2024-05-01 1230', False),
    ('555\n0199', False),  # Over lines: two numbers.
    # Each rule's edge: six digits; a group of one or five digits after the first; a first group of five; seven groups;
    # a colon or a slash; dates day first or month first only, with a two-digit year, from 1900; and what's no date.
    ('555-019', False), ('12 3 4567', False), ('555 01999', False), ('12345 555 0199', False),
    ('12 34 56 78 90 12 345', False), ('1234/5678', False), ('1234:5678', False), ('31.12.2024', False),
    ('12.31.2024', False), ('01.05.24 1230', False), ('19300501', False), ('13.13.2024', True), ('32.12.2024', True),
])
def test_what_is_a_distinctive_number(written, distinctive):
    assert bool(numbers(written)) == distinctive


@pytest.mark.parametrize('written, rare', [
    ('juniper', True), ('chemistry', True), ('bluefalcon77', True), ('surgery', True),
    ('chess', False), ('people', False), ('friday', False), ('wednesday', False), ('september', False), ('august', False),
    ('fourteenth', False), ('morning', False), ('weekend', False), ('tonight', False), ('hunter2', True),
])
def test_what_a_run_needs_a_rare_word_that_isnt_a_calendar_one(written, rare):
    assert bool([e for e in elements(f'a b c {written}') if e.startswith('4 ')]) == rare


def test_the_repro_an_edit_naming_a_year_retires_no_reply_about_another(world):  # noqa: F811
    client, store = world
    message = said(store, 'my diagnosis was in 2024')
    nearby(store, message, 'the olympics were held in 2024', 'your diagnosis was in 2024? thats a lot')
    withdraw(client, message, fingerprint=fingerprint('my diagnosis was in 2024', 'never mind', salt()))
    assert replies(store) == ['the olympics were held in 2024', controller.DRAWN]


# Sol round 1, B2 and B3: what the message says now, by the fingerprint's own kept list, was never taken out, whoever
# says it was: Fri compared a long edit's first 500 characters only, and two queued edits back to the original kept the
# first's removed list. The controller takes the kept list from all of it, its own copy included.
def long_edit(key, before=BEFORE):
    """As #388's round 0 sent an edit that only put 90 words before it: compared at 500 characters, kept in full."""
    now = 'hello '*90 + before
    printed = fingerprint(before, now[:500], key)
    printed['kept'] = fingerprint(None, now, key)['kept']
    return printed


def round_trip(key):
    """As #388's round 0 merged two edits queued before the first was taken: back to the original at the end."""
    first, second = fingerprint(BEFORE, 'never mind', key), fingerprint('never mind', BEFORE, key)
    return {'salt': key, 'kept': second['kept'], 'complete': True,
            'removed': sorted(set(first['removed']) | set(second['removed']))}


@pytest.mark.parametrize('sent', [long_edit, round_trip])
def test_what_the_message_says_now_is_never_taken_out(sent):
    printed = sent(salt())  # Each says the message says all of BEFORE now: nothing of it was taken out.
    assert removed(printed) == set()
    assert removed({**printed, 'complete': True}, [BEFORE]) == set()  # Nor by the controller's own copy.


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
@pytest.mark.parametrize('sent', [long_edit, round_trip])
def test_the_repro_a_reply_quoting_what_the_message_still_says_stays(world, how, sent):  # noqa: F811
    client, store = world
    message = said(store) if how == 'by its exchange' else source(age=60, channel='51')['id']
    nearby(store, message, QUOTE, UNRELATED)
    edited(client, how, message, sent(salt()))
    assert replies(store) == [QUOTE, UNRELATED]


# Sol round 2, R2-B3: a kept list cut short at MOST left out hashes the message still holds, and the controller read
# them as taken out of its own copy. The fingerprint says whether its kept list is whole (complete), and the controller
# reads its own copy only where it is.
def test_a_kept_list_cut_short_says_so(monkeypatch):
    count = len(elements(BEFORE))
    monkeypatch.setattr(fri_edit_fingerprint, 'MOST', count)
    assert fingerprint(None, BEFORE, salt())['complete'] is True  # Exactly MOST: all of it.
    monkeypatch.setattr(fri_edit_fingerprint, 'MOST', count-1)
    assert fingerprint(None, BEFORE, salt())['complete'] is False
    monkeypatch.undo()
    assert fingerprint(None, BEFORE, salt())['complete'] is True
    assert fingerprint(BEFORE, None, salt())['complete'] is False  # Nothing known of what it says now.


FILLER = ' '.join('filler' + chr(97+i % 26) + chr(97+i//26) for i in range(60))  # Sixty rare words: 57 runs.
CJK = ' '.join(chr(0x4E00+i) for i in range(1950))  # The repro's: 1,950 characters, which cut round 1's list short.


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
@pytest.mark.parametrize('now', [CJK + ' ' + BEFORE, FILLER + ' ' + BEFORE], ids=['the repro', 'a cut-short list'])
def test_the_r2_b3_repro_an_edit_that_kept_the_original_takes_nothing_out_of_the_controllers_copy(world, monkeypatch,  # noqa: F811
                                                                                              now, how):
    """The edit kept the whole original after a long run of other text. Its kept list was cut short where it had more
    elements than MOST (here 40), and the controller read the rest of its own copy as taken out: the replies quoting
    what it still says stay, whole or cut short. The repro's 1,950 CJK characters are no rare words now, so its list is
    whole; the filler's sixty rare words cut it short."""
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, 'number 4217 right')
    monkeypatch.setattr(fri_edit_fingerprint, 'MOST', 40)
    printed = fingerprint(None, now, salt())
    assert printed['complete'] is now.startswith(CJK)
    edited(client, how, message, printed)
    assert replies(store) == [QUOTE, 'number 4217 right']


def test_an_older_fingerprint_that_doesnt_say_takes_nothing_out_of_the_controllers_copy(world):  # noqa: F811
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    printed = fingerprint(None, AFTER, salt())
    del printed['complete']
    withdraw(client, message, fingerprint=printed)
    assert replies(store) == [QUOTE, UNRELATED]


# Sol round 2's own corpus, rebuilt from its repros and categories: 13 messages with something sensitive edited out, a
# reply quoting each, and 28 unrelated replies, by kind. Main retires none of them. Unrelated retired must stay 0; the
# quotes retired are what precision first leaves (see the PR for recall).
SENSITIVE = [
    ('my surgery is on May 1, 2024 at st marys hospital', 'so your surgery is on may 1 at st marys hospital?'),
    ('call me at 555-0199 after six', 'calling 555-0199 now'),
    ('my therapist diagnosed me with bipolar disorder last week', 'your therapist diagnosed you with bipolar disorder?'),
    ('i live at 4217 juniper crescent apt 3', 'is 4217 juniper crescent the place with the dog'),
    ('my bank pin is 707815 dont tell anyone', 'lol your bank pin is 707815'),
    ('the wifi password is bluefalcon77 for the stream house', 'the wifi password is bluefalcon77 right'),
    ('my real name is gabriella morales btw', 'gabriella morales is a nice name'),
    ('i got fired from lockheed yesterday', 'sorry you got fired from lockheed'),
    ('my ssn is 078-05-1120 for the form', 'got it 078-05-1120'),
    ('my sister is pregnant but nobody knows yet', 'your sister is pregnant? congrats'),
    ('my email is jordan.rivers@example.com', 'emailing jordan.rivers@example.com now'),
    ('i am in recovery from alcohol addiction', 'proud of your recovery from alcohol addiction'),
    ('text me on +44 20 7946 0958 tonight', 'texting +44 20 7946 0958'),
]
SOL_UNRELATED = [
    # Calendar runs: named months, weekdays, ordinals, ISO and dotted dates (R2-B1).
    'the patch is on May 1, 2024', 'the tournament is on friday 2024-05-01', 'the event is on wednesday, may 1',
    'season starts on the first of may', 'the patch is on 1 may 2024', 'the stream is on may 1st at 9pm',
    'my cat was born on may 1, 2024 too', 'the update is on monday the first of september',
    # Dates with times, together, in one group, and over lines (R2-B2).
    'the patch is on 2024-05-01 12:30:15', 'servers restart 2024-05-01\n12:30:15', 'build 20240501123015 failed',
    'the raid is at 12:30 on may 1', 'logs say 2024-05-01T12:30:15Z', 'the timer ended at 1714521600',
    # Numbers that aren't distinctive: house numbers, scores, amounts, short codes.
    '4217 points last season', 'my high score is 2400', 'the code is 1234 for the lobby', 'it cost 1,234,567 gold',
    # Names, places and common words the sensitive messages share.
    'gabriella is streaming later', 'lockheed has a booth at the expo', 'is the hospital parking free',
    'my sister also plays this game', 'the stream house wifi is slow again', 'recovery items are in the shop',
    'number 4217 right', 'call me after six', 'your therapist sounds nice', 'nobody knows yet lol',
]


def test_sols_corpus_retires_no_unrelated_reply():
    for before, _ in SENSITIVE:
        printed = fingerprint(before, 'never mind', salt())
        assert [reply for reply in SOL_UNRELATED if holds(reply, printed['salt'], removed(printed))] == [], before


def test_sols_sensitive_quotes_retired_are_the_verbatim_runs_and_numbers():
    retired = []
    for before, quote in SENSITIVE:
        printed = fingerprint(before, 'never mind', salt())
        if holds(quote, printed['salt'], removed(printed)):
            retired.append(quote)
    assert retired == [quote for _, quote in SENSITIVE if quote in RETIRED]


RETIRED = {  # 8 of 13. The rest share no run of four words (a paraphrase, three words) or only a six-digit number.
    'so your surgery is on may 1 at st marys hospital?', 'calling 555-0199 now', 'the wifi password is bluefalcon77 right',
    'sorry you got fired from lockheed', 'got it 078-05-1120', 'emailing jordan.rivers@example.com now',
    'proud of your recovery from alcohol addiction', 'texting +44 20 7946 0958',
}


def test_a_fingerprint_holds_no_words_and_differs_with_each_salt():
    first, second = fingerprint(BEFORE, AFTER, salt()), fingerprint(BEFORE, AFTER, salt())
    for word in BEFORE.split():
        assert word.strip(',') not in str(first)
    assert first['removed'] and not set(first['removed']) & set(second['removed'])
    assert holds(QUOTE, first['salt'], removed(first)) and not holds(UNRELATED, first['salt'], removed(first))
