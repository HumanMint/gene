"""gene #150 and #246: an edit retires another member's saved reply only where the reply holds what the edit took out.

Member 20 says where they live, then edits it out. Member 30's saved reply, whose request carried the message
(`drew_on`), quoted the old words, and stayed usable: an edit withdrew only member 20's own exchange (gene #150 left the
others, since retiring every reply that carried a message breadcrumbs most of a busy channel after a typo fix). Now Fri
sends a fingerprint of what the edit took out (hashed, never words: see fri_edit_fingerprint), and a reply that carried
the message and holds enough of it keeps only a breadcrumb, as a deletion's does. An unrelated reply in the channel stays,
and so does one holding only what the edit kept. Where neither Fri nor the controller has the text from before the edit,
nothing more goes than on main.
"""
import pytest

from gene import store as controller
from gene.fri_edit_fingerprint import fingerprint, holds, removed, salt, weighed
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


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
def test_the_repro_a_reply_quoting_what_the_edit_took_out_keeps_only_a_breadcrumb(world, how):  # noqa: F811
    """The issue: member 30's reply quoting member 20's old words stayed usable after the edit, and reached their next
    request. Now it keeps a breadcrumb; their unrelated reply in the same channel stays."""
    client, store = world
    message = said(store)
    nearby(store, message, QUOTE, UNRELATED)
    key = salt()
    if how == 'by its exchange':
        assert withdraw(client, message, fingerprint=fingerprint(BEFORE, AFTER, key)).json()['removed'] == 1
    else:
        assert withdraw_saved(client, message, fingerprints={message: fingerprint(BEFORE, AFTER, key)}).status_code == 200
    assert replies(store) == [controller.DRAWN, UNRELATED]
    assert '4217' not in str(store.chat_context('123', 'public:51', '30'))  # Their next request reads none of it.


def test_a_close_paraphrase_goes_and_a_reply_holding_only_what_was_kept_stays(world):  # noqa: F811
    client, store = world
    message = said(store)
    nearby(store, message, PARAPHRASE, KEPT_ONLY, UNRELATED)
    withdraw(client, message, fingerprint=fingerprint(BEFORE, AFTER, salt()))
    assert replies(store) == [controller.DRAWN, KEPT_ONLY, UNRELATED]


def test_the_controllers_own_copy_is_read_where_fri_had_no_text_from_before(world):  # noqa: F811
    """Fri had the message neither in view nor in Discord's cache: it sends what the message says now, and the
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
                {**print_, 'other': 1}, {**print_, 'removed': ['ab'*8]*3001}):
        assert withdraw(client, '1', fingerprint=bad).status_code == 422
    assert client.post('/chat/turn/withdraw/saved', headers=headers('fri'), json={
        'guild_id': '123', 'message_ids': ['1'], 'fingerprints': {'2': print_}}).status_code == 422  # Not one sent.


# Made-up edits, each with what it says after, replies that quote or closely paraphrase what it took out, and replies in
# the same channel about something else.
EDITS = [
    ('my new address is 4217 juniper crescent, come by friday', 'come by friday',
     ['they said their new address is 4217 juniper crescent', 'so you live on juniper crescent now?']),
    ("ugh my landlord raised the rent to 2400 a month, i'm so done", "ugh rent went up, i'm so done",
     ['your landlord raised the rent to 2400? thats insane', 'rent at 2400 a month is wild']),
    ('i think i failed my chemistry midterm, please dont tell my parents', 'i think the midterm went ok',
     ["you think you failed the chemistry midterm? i won't tell your parents"]),
    ('my sister gabriella starts chemotherapy on monday', 'never mind', ["hope gabriella's chemotherapy goes okay"]),
    ("i'm moving to denver next month for the new job at lockheed", "i'm moving next month",
     ['denver sounds great, congrats on lockheed', "you're moving to denver next month?"]),
    ('call me at 555 0199 after six', 'call me after six', ['ok calling 555 0199 after six', 'your number is 5550199 right']),
    ('my discord password is hunter2 lol jk', 'jk', ['hunter2 is a terrible password']),
    ('the wifi password for the stream house is bluefalcon77', 'ask me for the wifi password',
     ['the stream house wifi is bluefalcon77, got it']),
    ('my diagnosis was in 2024, still processing it', 'still processing it', ['your diagnosis was in 2024? thats a lot']),
    ('i leave for surgery at 12:30 on 2024-05-01', 'never mind', ['good luck with surgery at 12:30']),
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
def test_quotes_and_close_paraphrases_hold_what_was_taken_out_and_replies_about_something_else_dont(before, after,
                                                                                                    quoting):
    """The thresholds: a quoted run with a name or a number, a number, or two names or rare words is enough; one name, a
    common phrase, or what the edit kept, isn't."""
    printed = fingerprint(before, after, salt())
    taken = removed(printed)
    assert [reply for reply in quoting if not holds(reply, printed['salt'], taken)] == []
    assert [reply for reply in ELSEWHERE if holds(reply, printed['salt'], taken)] == []


@pytest.mark.parametrize('before, after, reply', [
    ('i broke up with jordan last night', 'rough night', 'sorry about jordan'),  # One name.
    ('teh raid is at nine tonight', 'the raid is at nine tonight', 'teh raid lol'),  # A typo.
    # A pair with one rare word in it, which counts once.
    ('the wifi password for the stream house is bluefalcon77', 'ask me for the wifi password', 'stream house tour later'),
    ('the house party music was loud', 'never mind', 'music at the house tonight?'),  # Two everyday words.
])
def test_one_name_a_typo_or_everyday_words_are_not_enough(before, after, reply):
    printed = fingerprint(before, after, salt())
    assert not holds(reply, printed['salt'], removed(printed))


# Sol round 1, B1: a year, a time or a date alone weighed as much as a phone number, so an unrelated reply naming the
# same one was retired. Each needs words beside it now; a phone number, a house number or a code is still enough alone.
GENERIC = [
    ('my diagnosis was in 2024', 'the olympics were held in 2024'),  # The repros.
    ('i will leave at 12:30', 'the raid starts at 12:30'),
    ('my surgery is on 2024-05-01', 'the patch drops 2024-05-01'),
    ('my surgery is on 5/1/2024', 'the event is on 5/1/2024'),
    ('my surgery is on 01.05.24', 'the event is on 01.05.24'),
    ('my last day is may 5, 2024', 'the update ships may 5, 2024'),
    ('my shift ends at 1530', 'the stream starts at 1530'),
    ('my shift ends at 0930am', 'the stream starts at 0930am'),
    ('my shift ends at 12:30:15', 'the timer says 12:30:15'),
    ('i grew up in the 1990s', 'music peaked in the 1990s'),
    ('i got married on 20240501', 'the build is from 20240501'),
    ('i was born in 1998 and graduated in 2020', 'between 1998 and 2020 nothing happened'),
    ('i leave at 12:30 on 2024-05-01', 'raid at 12:30 on 2024-05-01'),  # Two of them, and only common words between.
]


@pytest.mark.parametrize('before, unrelated', GENERIC, ids=[edit[1] for edit in GENERIC])
def test_a_year_a_time_or_a_date_alone_is_not_enough(before, unrelated):
    printed = fingerprint(before, 'never mind', salt())
    assert not holds(unrelated, printed['salt'], removed(printed))


@pytest.mark.parametrize('before, quoting', [
    ('my diagnosis was in 2024, still processing it', 'your diagnosis was in 2024? thats a lot'),
    ('my diagnosis was in 2024, still processing it', 'a diagnosis in 2024 is still recent'),  # A rare word beside it.
    # A run of common words and the year counts as the year, and a rare word beside it is enough.
    ('my diagnosis was in 2024, still processing it', 'well it was in 2024, the diagnosis'),
    ('my diagnosis, 2024, was rough', 'diagnosis, 2024? rough'),  # Beside it, as a pair.
    ('i leave for surgery at 12:30 on 2024-05-01', 'good luck with surgery at 12:30'),
    ('call me at 555-0199 after six', 'calling 5550199 now'),  # A phone number, written either way.
    ('call me at (212) 555-0199', 'is 2125550199 your number'),
    ('my address is 4217 juniper crescent', 'number 4217 right?'),  # A house number: no year, no time.
    ('the door code is 2468', 'the code 2468 worked'),
    ('my pin is 70815', 'is it 70815'),
    ('my locker is 4217b', 'locker 4217b'),
])
def test_a_phone_number_a_house_number_or_a_year_with_words_beside_it_still_is(before, quoting):
    printed = fingerprint(before, 'never mind', salt())
    assert holds(quoting, printed['salt'], removed(printed))


@pytest.mark.parametrize('written, weight, plain', [
    ('1900', 1, True), ('1899', 2, False), ('2024', 1, True), ('2075', 1, True), ('1998 2020', 1, True),
    ('1530', 1, True), ('0959', 1, True), ('2359', 1, True), ('2400', 2, False), ('1260', 2, False),
    ('12:30', 1, True), ('12:30:15', 1, True), ('9:30', 0, False), ('123', 0, False), ('12:45', 1, True),
    ('23:45:59', 1, True), ('23:45:60', 2, False), ('12:0:30', 2, False), ('023:30', 2, False), ('12:30:15:99', 2, False),
    ('2024-05-01', 1, True), ('2024-12-31', 1, True), ('2024-13-01', 2, False), ('2024-05-32', 2, False),
    ('2024-00-01', 2, False), ('2024-05-00', 2, False),
    ('5/1/2024', 1, True), ('12/31/2024', 1, True), ('31/12/2024', 1, True), ('13/13/2024', 2, False),
    ('01.05.24', 1, True), ('32.05.24', 2, False), ('31.12.99', 1, True), ('12/31/99', 1, True), ('31.13.99', 2, False),
    ('may 5, 2024', 1, True), ('2024 05', 1, True),
    ('05 2024', 1, True), ('12 31', 1, True), ('31 12', 1, True), ('20240501', 1, True), ('20241301', 2, False),
    ('1990s', 1, True), ('0930am', 1, True), ('4217b', 2, False), ('4217', 2, False), ('2468', 2, False),
    ('70815', 2, False), ('555 0199', 2, False), ('5550199', 2, False), ('(212) 555-0199', 2, False),
    ('a1b2c3d4', 2, False), ('hunter2', 1, False), ('juniper', 1, False), ('chess', 0, False),
])
def test_what_a_number_or_a_word_weighs_alone(written, weight, plain):
    said, alone, dated = weighed(written)
    assert (alone[-1], dated[-1]) == (weight, plain)
    assert len(said) == (2 if written == 'may 5, 2024' else 1)


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
    return {'salt': key, 'kept': second['kept'], 'removed': sorted(set(first['removed']) | set(second['removed']))}


@pytest.mark.parametrize('sent', [long_edit, round_trip])
def test_what_the_message_says_now_is_never_taken_out(sent):
    printed = sent(salt())  # Each says the message says all of BEFORE now: nothing of it was taken out.
    assert removed(printed) == set()
    assert removed(printed, [BEFORE]) == set()  # Nor by the controller's own copy.


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
@pytest.mark.parametrize('sent', [long_edit, round_trip])
def test_the_repro_a_reply_quoting_what_the_message_still_says_stays(world, how, sent):  # noqa: F811
    client, store = world
    message = said(store) if how == 'by its exchange' else source(age=60, channel='51')['id']
    nearby(store, message, QUOTE, UNRELATED)
    printed = sent(salt())
    if how == 'by its exchange':
        withdraw(client, message, fingerprint=printed)
    else:
        withdraw_saved(client, message, fingerprints={message: printed})
    assert replies(store) == [QUOTE, UNRELATED]


def test_a_fingerprint_holds_no_words_and_differs_with_each_salt():
    first, second = fingerprint(BEFORE, AFTER, salt()), fingerprint(BEFORE, AFTER, salt())
    for word in BEFORE.split():
        assert word.strip(',') not in str(first)
    assert first['removed'] and not set(first['removed']) & set(second['removed'])
    assert holds(QUOTE, first['salt'], removed(first)) and not holds(UNRELATED, first['salt'], removed(first))
