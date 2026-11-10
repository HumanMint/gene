"""Structural saved-answer ancestry, including question-only history and old clients."""
import pytest

from gene.store import DRAWN, Store
from tests.test_turn_withdrawal import save
from tests.test_withdrawal_sources import world  # noqa: F401, F811


def replies(store, user):
    return store.chat_context('123', 'public:51', user)['turns']


def test_second_and_third_hop_keep_question_only_turn(store):
    a = save(store, 'A', user='20', reply_message_id='901')
    save(store, 'B', user='30', drew_on=[a], reply_message_id='902')
    b = replies(store, '30')[0]
    assert b['answer_id'] == '902'
    save(store, 'C', user='40', carried_answers=['902'], reply_message_id='903')
    save(store, 'D', user='50', carried_answers=['903'], reply_message_id='904')
    save(store, 'question only', user='60', drew_on=[b['source']['id']], carried_answers=[])
    store.withdraw_saved('123', [a], deleted=True)
    store = Store(store.path)
    assert replies(store, '30')[0]['reply'] == DRAWN
    assert replies(store, '40')[0]['reply'] == DRAWN
    assert replies(store, '50')[0]['reply'] == DRAWN
    assert replies(store, '60')[0]['reply'] == 'okay'


@pytest.mark.parametrize('value', [None, '902', {}, [None], [True], ['bad'], list(map(str, range(1001)))])
def test_malformed_optional_fails_open(store, value):
    save(store, 'kept', carried_answers=value)
    store.withdraw_saved('123', ['999'], deleted=True)
    assert replies(store, '20')[0]['reply'] == 'okay'


def test_old_fri_behavior(store):
    a = save(store, 'A', reply_message_id='901')
    save(store, 'B', user='30', drew_on=[a], reply_message_id='902')
    save(store, 'legacy C', user='40', drew_on=[replies(store, '30')[0]['source']['id']])
    store.withdraw_saved('123', [a], deleted=True)
    assert replies(store, '30')[0]['reply'] == DRAWN
    assert replies(store, '40')[0]['reply'] == 'okay'


def test_bounds_and_indexed_lookup(store, monkeypatch):
    from gene import saved_answers
    save(store, 'B', reply_message_id='902')
    save(store, 'C', user='30', carried_answers=['902'], reply_message_id='903')
    monkeypatch.setattr(saved_answers, 'MAX_DEPTH', 1)
    said = save(store, 'too deep', user='40', carried_answers=['903'])
    assert replies(store, '40') == []
    store.withdraw_saved('123', [said], deleted=True)
    with store.connection() as db:
        plan = ' '.join(str(tuple(row)) for row in db.execute(
            'EXPLAIN QUERY PLAN SELECT turn_id FROM chat_answer_ancestors WHERE guild_id=? AND answer_id=?',
            ('123', '902')))
        assert 'SEARCH' in plan and 'INDEX' in plan
        # Additive tables clean themselves up even under an old controller's ordinary SQL.
        db.execute('DELETE FROM chat_turns WHERE user_id=?', ('30',))
        assert not db.execute('SELECT 1 FROM chat_answer_ancestors').fetchone()


@pytest.mark.parametrize('value', [None, 3, '902', {}, [None], ['invalid']])
def test_api_malformed_field_keeps_save(world, value):  # noqa: F811
    from tests.test_api import headers
    from tests.test_fri_endings import source
    client, store = world
    response = client.post('/chat/turn', headers=headers('fri'), json={
        'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20', 'revision': 0,
        'input': 'hello', 'reply': 'okay', 'model': 'model', 'effort': 'high',
        'source': source(channel='51'), 'provenance': [], 'carried_answers': value})
    assert response.status_code == 200, response.text
    assert replies(store, '20')[0]['reply'] == 'okay'
