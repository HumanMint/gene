"""gene #149 item 1: a reply that read a retired reply from saved history keeps its words.

Member 20 says something, and member 30 asks Fri about it: Fri's reply B carries member 20's message (its `drew_on`
names it). Later member 30 asks again, and Fri reads B back from their saved history: that turn's `drew_on` names B's
exchange by its own message, as Fri names saved context it carried, not B's reply. Delete member 20's message: B keeps
a breadcrumb, but the later reply, which may repeat what B said, kept its words and stayed verbatim, since retirement
followed only Fri's replies' messages from one turn to the next.
"""
import pytest

from gene import store as controller
from tests.test_fri_endings import source
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import withdraw
from tests.test_withdrawal_sources import MARKER, learn, replies, turn, world  # noqa: F401 (the fixture)


def history(store):
    """Member 20's message, member 30's first reply about it (B), and their later turn that read B from saved
    history; and an unrelated turn of theirs."""
    taken, _ = learn(store, 'explain '+MARKER, age=40)
    first = turn(store, 'what did they mean', drew_on=[taken], reply_id=str(int(taken)+1), age=30)
    turn(store, 'say that again', drew_on=[first], reply='again: '+MARKER, age=20)  # B, read from saved history.
    turn(store, 'anything else', drew_on=[], reply='nope', age=10)
    return taken, first


@pytest.mark.parametrize('how', ['by its exchange', 'by its ID'])
def test_the_repro_a_reply_that_read_a_retired_one_from_saved_history_keeps_only_a_breadcrumb(world, how):  # noqa: F811
    client, store = world
    taken, _ = history(store)
    if how == 'by its exchange':
        withdraw(client, taken, deleted=True)
    else:
        withdraw_saved(client, taken, deleted=True)
    assert replies(store) == [controller.DRAWN, controller.DRAWN, 'nope']
    assert MARKER not in str(store.chat_context('123', 'public:51', '30'))


def test_a_third_hop_through_saved_history_goes_too(world):  # noqa: F811
    client, store = world
    taken, first = history(store)
    with store.connection() as db:
        second = db.execute("SELECT json_extract(source,'$.id') FROM chat_turns WHERE input='say that again'").fetchone()[0]
    turn(store, 'and once more', drew_on=[second], reply='once more: '+MARKER, age=5)  # Read the later one back.
    withdraw_saved(client, taken, deleted=True)
    assert replies(store) == [controller.DRAWN, controller.DRAWN, 'nope', controller.DRAWN]


def test_another_members_turn_that_only_had_the_question_in_view_stays(world):  # noqa: F811
    """Only the member's own turns in that conversation read its saved history. Another member's request that carried
    member 30's question (not Fri's reply) in its live view read no retired words through it."""
    client, store = world
    taken, first = history(store)
    turn(store, 'what is going on', user='40', drew_on=[first], reply='no idea', age=15)
    turn(store, 'elsewhere', conversation='public:52', drew_on=[first], reply='over here', age=12)  # Theirs elsewhere.
    withdraw_saved(client, taken, deleted=True)
    assert replies(store, '40') == ['no idea'] and replies(store, conversation='public:52') == ['over here']


def test_an_earlier_message_of_a_retired_exchanges_burst_counts_as_its_own(world):  # noqa: F811
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=40)
    first, last = source(age=31, user='30', channel='51'), source(age=30, user='30', channel='51')
    store.save_chat('123', 'public:51', '30', 0, 'wait', 'they said '+MARKER, 'model', 'high', source=last,
                    provenance=[{'user_id': '20', 'revision': 0}], verbatim=True, drew_on=[taken],
                    burst=[{'source': first, 'text': 'what did they mean'}, {'source': last, 'text': 'wait'}])
    turn(store, 'say that again', drew_on=[first['id']], reply='again: '+MARKER, age=20)
    withdraw_saved(client, taken, deleted=True)
    assert replies(store) == [controller.DRAWN, controller.DRAWN]


def test_an_unreadable_drew_on_or_burst_never_stops_the_withdrawal(world):  # noqa: F811
    """Gene writes both with json.dumps; one hand-edited or cut short is read as naming nothing, and the rest goes.
    By its exchange: by ID, main's own lookup of what holds a message reads the burst first, as before."""
    client, store = world
    taken, first = history(store)
    with store.connection() as db:
        db.execute("UPDATE chat_turns SET burst='{not json' WHERE input='what did they mean'")
        # Cut short, but naming the exchange read back and the message withdrawn: a quick match finds it, and reading
        # it fails. On main, the reply hop's own read raised on one like it.
        db.execute('UPDATE chat_turns SET drew_on=? WHERE input=?', (f'["{first}", "{taken}"', 'anything else'))
    assert withdraw(client, taken, deleted=True).status_code == 200
    with store.connection() as db:  # Read as stored: a history read parses the burst, as main does.
        assert [row[0] for row in db.execute("SELECT reply FROM chat_turns WHERE user_id='30' ORDER BY id")] == [
            controller.DRAWN, controller.DRAWN, 'nope']


def test_an_edit_leaves_both_as_on_main(world):  # noqa: F811
    """gene #150: an edit leaves replies that carried the message, at either hop."""
    client, store = world
    taken, _ = history(store)
    withdraw_saved(client, taken)
    assert replies(store) == ['about that: '+MARKER, 'again: '+MARKER, 'nope']
