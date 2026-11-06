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
from gene.fri_edit_fingerprint import fingerprint, holds, removed, salt
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
]
ELSEWHERE = [
    'the raid starts at nine, bring potions', 'lol same, rent is brutal everywhere', 'anyone up for ranked tonight',
    "i'm so done with this patch honestly", 'gg everyone, that was a good run', 'what time is the stream tomorrow',
    'my cat knocked over my coffee again', 'come by friday works for me', 'that midterm sounds rough, good luck',
    'next month is going to be busy', 'the new job sounds exciting', 'honestly i feel the same about mondays',
    'call me when you get home', 'the wifi is slow again', 'denver is cold this time of year',
    'my sister also plays this game', 'please dont tell anyone i lost', 'password resets are annoying',
    'is the chemistry lab open', 'lockheed has a booth at the expo', 'lets queue after six',
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


def test_a_fingerprint_holds_no_words_and_differs_with_each_salt():
    first, second = fingerprint(BEFORE, AFTER, salt()), fingerprint(BEFORE, AFTER, salt())
    for word in BEFORE.split():
        assert word.strip(',') not in str(first)
    assert first['removed'] and not set(first['removed']) & set(second['removed'])
    assert holds(QUOTE, first['salt'], removed(first)) and not holds(UNRELATED, first['salt'], removed(first))
