"""Reply checks keep abstract findings about unhappy follow-ups, never chat text or who said it."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from gene import fri_reply_checks
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_endings import source

ASH = {'user_id': '30', 'revision': 0}
FINDING = {'category': 'wrong_info', 'severity': 'high',
           'issue': 'gave a wrong release date for a game and stuck with it',
           'fix': 'check current sources before stating dates'}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(tmp_path/'gene.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='456', fri_token='fri-'+'x'*32,
                        agent_channels='1,2')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    agent = AgentStore(store, settings)
    agent.update({'enabled': True, 'reply_check_enabled': True}, 0, '10')
    return store, agent, fri_reply_checks.ReplyChecks(agent), settings


def save(store, text, reply='it comes out in march', user='20', conversation='public:1', ago=0, **extra):
    store.save_chat('123', conversation, user, extra.pop('revision', 0), text, reply, 'model', 'high',
                    source=source(user=user, channel=conversation.split(':')[1]),
                    provenance=extra.pop('provenance', []), **extra)
    with store.connection() as db:
        turn = db.execute('SELECT max(id) FROM chat_turns').fetchone()[0]
        if ago:
            db.execute('UPDATE chat_turns SET at=at-? WHERE id=?', (ago, turn))
    return turn


def exchange(store, user='20', conversation='public:1', gap=60, **extra):
    """Fri answers, then the member pushes back."""
    save(store, 'when does the new zelda come out', user=user, conversation=conversation, ago=gap,
         verbatim=True, provenance=extra.pop('earlier_provenance', []))
    return save(store, 'no it was delayed, that date is wrong', 'oh my bad, it moved to may', user=user,
                conversation=conversation, verbatim=True, **extra)


def due(agent):
    with agent.store.connection() as db:
        agent.put(db, 'next_reply_check', 0)


def flag(checks, claim, **finding):
    return checks.finish(claim['id'], [{**FINDING, 'pair_id': claim['pairs'][0]['id'], **finding}], 'completed')


def findings(store):
    with store.connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM fri_reply_findings')]


def test_follow_up_pairs_are_offered_without_identities(setup):
    store, agent, checks, _ = setup
    with store.connection() as db:  # Turns from before enablement are never offered.
        agent.put(db, 'reply_check_cursor', None)
    exchange(store)
    assert checks.claim() is None
    due(agent)
    later = exchange(store, user='21')
    claim = checks.claim()
    assert [pair['id'] for pair in claim['pairs']] == [str(later)]
    pair = claim['pairs'][0]
    assert set(pair) == {'id', 'gap_seconds', 'earlier', 'later'} and 55 <= pair['gap_seconds'] <= 65
    assert pair['earlier'] == {'member': 'when does the new zelda come out', 'fri': 'it comes out in march'}
    assert pair['later']['member'] == 'no it was delayed, that date is wrong'
    assert '21' not in json.dumps(claim) and claim['model'] == 'gpt-6-luna' and claim['effort'] == 'low'


def test_only_real_follow_ups_to_fri_replies_qualify(setup):
    store, agent, checks, _ = setup
    exchange(store, gap=fri_reply_checks.FOLLOW_UP+60)  # Too late to be about that reply.
    exchange(store, conversation='private:9')  # DMs stay out unless the owner opts in.
    save(store, 'nice', '', user='40', response_kind='react', reaction='👍', verbatim=False)
    save(store, 'that is wrong', user='40', verbatim=True)  # Follows a reaction, not a reply.
    save(store, 'what is the date', user='50', verbatim=False)  # A breadcrumb, not what they saw.
    save(store, 'wrong', user='50', verbatim=True)
    assert checks.claim() is None
    agent.update({'reply_check_private': True}, 1, '10')
    due(agent)
    exchange(store, conversation='private:9')
    assert len(checks.claim()['pairs']) == 1


def test_abstract_findings_are_kept_and_quotes_links_and_ids_are_not(setup):
    store, agent, checks, _ = setup
    exchange(store)
    claim = checks.claim()
    assert flag(checks, claim) == {'kept': 1, 'pairs': 1}
    row, = findings(store)
    assert (row['category'], row['severity'], row['space'], row['issue']) == (
        'wrong_info', 'high', 'public', FINDING['issue'])
    for issue in ('member said no it was delayed, that date is wrong', 'see https://example.com',
                  'told <@12345678901234567> off', 'ignored @ash', 'wrong id 1234567890'):
        due(agent)
        exchange(store, user='60')
        assert flag(checks, checks.claim(), issue=issue)['kept'] == 0
    due(agent)
    exchange(store, user='70')
    claim = checks.claim()
    pair = claim['pairs'][0]['id']
    result = checks.finish(claim['id'], [{**FINDING, 'pair_id': pair}, {**FINDING, 'pair_id': pair},
                                         {**FINDING, 'pair_id': '999'}, {**FINDING, 'pair_id': pair, 'category': 'made_up'}],
                           'completed')
    assert result['kept'] == 1
    with pytest.raises(ValueError):
        checks.finish(claim['id'], [], 'completed')


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats', 'enable'])
def test_any_privacy_change_removes_findings_about_the_member(setup, erase):
    store, agent, checks, _ = setup
    exchange(store)
    exchange(store, user='40', earlier_provenance=[ASH])
    claim = checks.claim()
    checks.finish(claim['id'], [{**FINDING, 'pair_id': pair['id']} for pair in claim['pairs']], 'completed')
    assert len(findings(store)) == 2
    for member in ('20', '30'):  # The speaker, then someone the second pair drew on.
        if erase == 'erase_user':
            store.erase_user(member)
        elif erase == 'clear_chats':
            store.clear_chats(member)
        else:
            store.personal_control(member, erase)
    assert findings(store) == [] and checks.overview()['flagged'] == 0


def test_revision_change_hides_findings_and_privacy_change_mid_check_drops_them(setup):
    store, agent, checks, _ = setup
    exchange(store)
    flag(checks, checks.claim())
    with store.connection() as db:  # A revision change without an eager delete, e.g. a future path.
        db.execute('INSERT INTO chat_revisions VALUES (?,1)', ('20',))
    assert checks.overview()['flagged'] == 0
    due(agent)
    exchange(store, user='40')
    claim = checks.claim()
    store.clear_chats('40')
    assert flag(checks, claim)['kept'] == 0


def test_failed_checks_retry_then_skip_and_refund_the_allowance(setup):
    store, agent, checks, _ = setup
    exchange(store)
    claim = checks.claim()
    for attempt in range(3):
        if attempt:
            due(agent)
            claim = checks.claim()
            assert claim and len(claim['pairs']) == 1
        checks.finish(claim['id'], [{**FINDING, 'pair_id': claim['pairs'][0]['id']}], 'failed')
        with store.connection() as db:  # Retries are free; the skipped batch keeps its allowance.
            assert agent.get(db, 'reply_check_budget')['used'] == (attempt == 2)
    due(agent)
    assert checks.claim() is None and findings(store) == []


def test_daily_allowance_and_interrupted_claims(setup, monkeypatch):
    store, agent, checks, _ = setup
    agent.update({'reply_check_daily_pairs': 10}, 1, '10')
    for user in range(12):
        exchange(store, user=str(100+user))
    assert len(checks.claim()['pairs']) == 10
    due(agent)
    assert checks.claim() is None  # One claim at a time.
    clock = fri_reply_checks.time.time()
    monkeypatch.setattr(fri_reply_checks.time, 'time', lambda: clock+601)
    due(agent)
    assert len(checks.claim()['pairs']) == 10  # The interrupted batch is retried within the allowance.
    with store.connection() as db:
        run = db.execute('SELECT status,pairs FROM fri_reply_check_runs').fetchone()
        assert tuple(run) == ('interrupted', 10)


def test_overview_and_export_never_name_members_channels_or_messages(setup, tmp_path):
    store, agent, checks, settings = setup
    exchange(store, conversation='public:777777')
    flag(checks, checks.claim())
    overview = checks.overview()
    assert overview['flagged'] == 1 and overview['categories'] == {'wrong_info': 1} and overview['checked_pairs'] == 1
    exported = fri_reply_checks.export(settings.db, 7)
    text = json.dumps(exported)
    assert exported['findings'][0]['issue'] == FINDING['issue']
    assert not any(value in text for value in ('777777', '"20"', 'zelda', 'delayed'))
    fri_reply_checks.purge(settings.db)
    with sqlite3.connect(settings.db) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='fri_reply_findings'").fetchone()


def test_api_is_service_authenticated_and_overview_is_owner_only(tmp_path):
    settings = Settings(tmp_path/'api.db', 'guild', 'owner', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='workshop', fri_token='fri-'+'x'*32)
    client = TestClient(create_app(settings))
    assert client.post('/agent/reply-checks/claim', headers=headers('runtime')).status_code == 401
    assert client.post('/agent/reply-checks/claim', headers=headers('fri')).json() is None
    owner, member = {'guild_id': 'guild', 'user_id': 'owner'}, {'guild_id': 'guild', 'user_id': 'member'}
    assert client.post('/agent/reply-checks/overview', headers=headers('fri'), json=member).status_code == 403
    assert client.post('/agent/reply-checks/overview', headers=headers('fri'), json=owner).json()['flagged'] == 0
    body = {'findings': [], 'status': 'completed'}
    assert client.post('/agent/reply-checks/'+'a'*32+'/finish', headers=headers('fri'), json=body).status_code == 400
    assert client.post('/agent/reply-checks/nope/finish', headers=headers('fri'), json=body).status_code == 404
    usage = {'attempt_id': 'b'*32, 'model': 'gpt-6-luna', 'effort': 'low', 'status': 'completed',
             'counters': {}, 'role': 'reply_check'}
    assert client.post('/chat/usage/report', headers=headers('fri'), json=usage).status_code == 200
