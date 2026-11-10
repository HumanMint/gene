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
    assert replies(store, '30')[0]['answer_id'] is None
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
    assert replies(store, '30')[0]['answer_id'] is None
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


def test_fallback_identity_is_stable_and_never_reused(store):
    message = save(store, 'first')
    first = replies(store, '20')[0]['answer_id']
    assert first.startswith('turn:')
    store = Store(store.path)
    assert replies(store, '20')[0]['answer_id'] == first
    store.withdraw_saved('123', [message], deleted=True)
    save(store, 'replacement')
    assert replies(store, '20')[0]['answer_id'] != first


@pytest.mark.parametrize('cause', ['summary', 'withdrawal', 'erasure'])
def test_upgrade_legacy_retired_answer_has_no_identity(store, cause):
    from gene import fri_member_summary as summary
    a = save(store, 'A', user='20', reply_message_id='901')
    b = save(store, 'B', user='30', drew_on=[a], reply_message_id='902', summary_generation=0,
             summary_dependencies=[{'user_id': '20', 'generation': 0}])
    with store.connection() as db:
        # 29d6791 had neither this column nor the retirement trigger. Its retirement SQL leaves a breadcrumb.
        db.execute('DROP TRIGGER chat_answer_retired')
        db.execute('ALTER TABLE chat_turns DROP COLUMN answer_retired')
        if cause == 'summary':
            summary.advance(db, '123', '30')
        elif cause == 'withdrawal':
            Store._retire_drawn(db, '123', [a])
        else:
            summary.erase(db, '20')
    upgraded = Store(store.path)
    turn = replies(upgraded, '30')[0]
    assert turn['answer_id'] is None
    with upgraded.connection() as db:
        assert db.execute('SELECT answer_retired FROM chat_turns WHERE reply_message_id=?', ('902',)).fetchone()[0]
        # An old writer or damaged lifecycle flag cannot make a breadcrumb an answer again.
        db.execute('UPDATE chat_turns SET answer_retired=0 WHERE reply_message_id=?', ('902',))
    turn = replies(upgraded, '30')[0]
    assert turn['answer_id'] is None
    save(upgraded, 'question and breadcrumb only', user='40', drew_on=[b],
         carried_answers=[turn['answer_id']] if turn['answer_id'] else [])
    upgraded.withdraw_saved('123', [a], deleted=True)
    assert replies(upgraded, '40')[0]['reply'] == 'okay'


def test_trimmed_middle_turn_preserves_answer_message_ancestry(store):
    a = save(store, 'A', user='20', reply_message_id='901')
    b = save(store, 'B', user='30', drew_on=[a], reply_message_id='902')
    save(store, 'C', user='30', carried_answers=['902'], reply_message_id='903')
    save(store, 'question only', user='40', drew_on=[b])
    for i in range(5):
        save(store, f'newer {i}', user='30')
    assert all(t['input'] != 'B' for t in replies(store, '30'))
    store = Store(store.path)
    store.withdraw_saved('123', [a], deleted=True)
    assert next(t for t in replies(store, '30') if t['input'] == 'C')['reply'] == DRAWN
    assert replies(store, '40')[0]['reply'] == 'okay'


def test_exact_retirement_words_delivered_verbatim_are_still_an_answer(store):
    from gene.saved_answers import retirement_markers
    for i, marker in enumerate(retirement_markers()):
        store.save_chat('123', 'public:51', str(20+i), 0, 'quote this exact marker', marker, 'model', 'high',
                        reply_message_id=str(900+i), verbatim=True, provenance=[])
    store = Store(store.path)
    assert [replies(store, str(20+i))[0]['answer_id'] for i in range(len(retirement_markers()))] == [
        str(900+i) for i in range(len(retirement_markers()))]


def test_answer_message_cap_marks_unknown_and_holds_reuse(store, monkeypatch):
    from gene import saved_answers
    monkeypatch.setattr(saved_answers, 'MAX_MESSAGES', 2)
    save(store, 'B', user='20', drew_on=['900', '901', '902'], reply_message_id='903', verbatim=True)
    save(store, 'C', user='30', carried_answers=['903'], reply_message_id='904', verbatim=True)
    save(store, 'D', user='40', carried_answers=['904'], reply_message_id='905', verbatim=True)
    for user, answer in [('30', '904'), ('40', '905')]:
        turn = replies(store, user)[0]
        assert turn['answer_id'] is None and turn['reply'] == saved_answers.UNKNOWN
        assert store.message_sources('123', [answer])[answer]['verbatim'] is False
        with store.connection() as db:
            kept = db.execute('SELECT reply,verbatim,answer_messages_unknown FROM chat_turns WHERE user_id=?',
                              (user,)).fetchone()
            assert tuple(kept) == ('okay', 1, 1)  # Mark uncertainty without discarding the delivered answer.
            assert db.execute('SELECT count(*) FROM chat_answer_messages a JOIN chat_turns t ON t.id=a.turn_id '
                              'WHERE t.user_id=?', (user,)).fetchone()[0] <= 2
    # Question-only input does not inherit either a bounded set or its unknown marker.
    save(store, 'question only', user='50', drew_on=['903'])
    assert replies(store, '50')[0]['answer_id'] is not None


def test_inherited_message_lookup_is_indexed_and_cleaned_on_trim(store):
    save(store, 'B', reply_message_id='902', drew_on=['900'])
    save(store, 'C', user='30', carried_answers=['902'])
    with store.connection() as db:
        plan = ' '.join(str(tuple(row)) for row in db.execute(
            'EXPLAIN QUERY PLAN SELECT turn_id FROM chat_answer_messages WHERE guild_id=? AND message_id=?',
            ('123', '900')))
        assert 'SEARCH' in plan and 'INDEX' in plan
        db.execute('DELETE FROM chat_turns WHERE user_id=?', ('30',))
        assert not db.execute('SELECT 1 FROM chat_answer_messages').fetchone()
