"""Reply checks keep labelled findings about unhappy follow-ups, never chat text or who said it."""

import dataclasses
import json
import sqlite3
import time

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
FINDING = {'category': 'wrong_info', 'severity': 'high', 'topic': 'games', 'fix': 'verify_facts'}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(tmp_path/'gene.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='456', fri_token='fri-'+'x'*32,
                        agent_channels='1,2,777777')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    agent = AgentStore(store, settings)
    agent.update({'enabled': True, 'reply_check_enabled': True}, 0, '10')
    with store.connection() as db:  # As if checks were switched on yesterday.
        agent.put(db, 'reply_check_since', time.time()-86400)
        agent.put(db, 'reply_check_cursor', None)
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
    later = exchange(store, user='424242')
    claim = checks.claim()
    assert [pair['id'] for pair in claim['pairs']] == [str(later)]
    pair = claim['pairs'][0]
    assert set(pair) == {'id', 'gap_seconds', 'earlier', 'later'} and 55 <= pair['gap_seconds'] <= 65
    assert pair['earlier'] == {'member': 'when does the new zelda come out', 'fri': 'it comes out in march'}
    assert pair['later']['member'] == 'no it was delayed, that date is wrong'
    assert '424242' not in json.dumps(claim) and claim['model'] == 'gpt-6-luna' and claim['effort'] == 'low'


def test_neither_turn_from_before_checks_started_is_offered(setup):
    store, agent, checks, _ = setup
    save(store, 'when does the new zelda come out', verbatim=True)
    agent.update({'enabled': False}, 1, '10')  # Restarting either switch moves the boundary.
    agent.update({'enabled': True}, 2, '10')
    save(store, 'no that date is wrong', verbatim=True)  # Its earlier half predates the restart.
    assert checks.claim() is None
    with store.connection() as db:
        since = agent.get(db, 'reply_check_since')
    agent.update({'reply_check_minutes': 60}, 3, '10')  # Other changes keep it.
    due(agent)
    later = exchange(store, user='21', gap=0)
    assert [pair['id'] for pair in checks.claim()['pairs']] == [str(later)]
    with store.connection() as db:
        assert agent.get(db, 'reply_check_since') == since


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


def test_only_conversations_fri_may_chat_in_now_are_checked(setup):
    store, agent, checks, settings = setup
    exchange(store, conversation='public:3')  # Not a configured channel (or a thread under one).
    assert checks.claim() is None
    due(agent)
    exchange(store, conversation='public:2')
    claim = checks.claim()
    narrowed = dataclasses.replace(settings, agent_channels='1')  # Restarted without channel 2.
    restarted = fri_reply_checks.ReplyChecks(AgentStore(store, narrowed))
    assert restarted.finish(claim['id'], [{**FINDING, 'pair_id': claim['pairs'][0]['id']}], 'completed')['kept'] == 0


def test_a_reused_turn_id_does_not_stand_in_for_the_checked_turn(setup):
    store, agent, checks, _ = setup
    later = exchange(store)
    claim = checks.claim()
    with store.connection() as db:  # Pruned, then the id handed to someone else's newer turn.
        db.execute("UPDATE chat_turns SET user_id='99', at=at+5 WHERE id=?", (later,))
    assert flag(checks, claim)['kept'] == 0


def test_a_retry_cut_short_by_the_allowance_keeps_the_rest_of_its_attempts(setup, monkeypatch):
    store, agent, checks, _ = setup
    agent.update({'reply_check_daily_pairs': 30}, 1, '10')
    for user in range(20):
        exchange(store, user=str(100+user))
    sent = {}

    def claim():
        due(agent)
        claim = checks.claim()
        for pair in claim['pairs'] if claim else ():
            sent[pair['id']] = sent.get(pair['id'], 0)+1
        return claim
    checks.finish(claim()['id'], [], 'failed')
    retry = claim()
    assert len(retry['pairs']) == 10  # Only 10 left in today's allowance.
    checks.finish(retry['id'], [], 'completed')
    clock = fri_reply_checks.time.time()
    monkeypatch.setattr(fri_reply_checks.time, 'time', lambda: clock+86400)
    for _ in range(2):  # The other 10 already failed once: two more tries, not three.
        rest = claim()
        assert len(rest['pairs']) == 10
        checks.finish(rest['id'], [], 'failed')
    assert claim() is None and sorted(sent.values()) == [2]*10+[3]*10


def test_findings_are_fixed_labels_and_free_text_is_never_kept(setup):
    store, agent, checks, settings = setup
    exchange(store)
    claim = checks.claim()
    assert flag(checks, claim) == {'kept': 1, 'pairs': 1}
    row, = findings(store)
    assert {key: row[key] for key in ('category', 'severity', 'topic', 'fix', 'space')} == {**FINDING, 'space': 'public'}
    assert set(row) == {'id', 'at', 'turn_at', 'user_id', 'revision', 'provenance', 'space', *FINDING}
    due(agent)
    exchange(store, user='70')
    claim = checks.claim()
    pair = claim['pairs'][0]['id']
    made_up = [{**FINDING, 'pair_id': pair, key: 'alice has the flu'} for key in FINDING]
    result = checks.finish(claim['id'], [*made_up, {**FINDING, 'pair_id': '999'}, {**FINDING, 'pair_id': pair},
                                         {**FINDING, 'pair_id': pair}], 'completed')
    assert result['kept'] == 1
    with pytest.raises(ValueError):
        checks.finish(claim['id'], [], 'completed')
    due(agent)
    exchange(store, user='80')
    client, claim = TestClient(create_app(settings)), checks.claim()
    body = {'findings': [{**FINDING, 'pair_id': claim['pairs'][0]['id'], 'issue': 'alice said the date was wrong'}],
            'status': 'completed'}
    assert client.post(f"/agent/reply-checks/{claim['id']}/finish", headers=headers('fri'), json=body).json()['kept'] == 1
    assert 'alice' not in json.dumps(findings(store))


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


def test_failed_checks_retry_then_skip_and_every_attempt_counts(setup):
    store, agent, checks, _ = setup
    exchange(store)
    claim = checks.claim()
    for attempt in range(3):
        if attempt:
            due(agent)
            claim = checks.claim()
            assert claim and len(claim['pairs']) == 1
        checks.finish(claim['id'], [{**FINDING, 'pair_id': claim['pairs'][0]['id']}], 'failed')
        with store.connection() as db:  # Every model call spends allowance, retries included.
            assert agent.get(db, 'reply_check_budget')['used'] == attempt+1
    due(agent)
    assert checks.claim() is None and findings(store) == []


def test_a_retry_is_the_same_batch_and_new_turns_get_their_own_attempts(setup):
    store, agent, checks, _ = setup
    first = exchange(store)
    claim = checks.claim()
    checks.finish(claim['id'], [], 'failed')
    second = exchange(store, user='21')
    for _ in range(2):  # Two more tries for the first batch alone, then it's skipped.
        due(agent)
        claim = checks.claim()
        assert [pair['id'] for pair in claim['pairs']] == [str(first)]
        checks.finish(claim['id'], [], 'failed')
    due(agent)
    claim = checks.claim()
    assert [pair['id'] for pair in claim['pairs']] == [str(second)]
    checks.finish(claim['id'], [], 'failed')
    due(agent)
    assert [pair['id'] for pair in checks.claim()['pairs']] == [str(second)]


def test_equal_timestamps_are_neither_skipped_nor_repeated(setup, monkeypatch):
    store, agent, checks, _ = setup
    monkeypatch.setattr(fri_reply_checks, 'BATCH', 1)
    turns = [exchange(store, user=user) for user in ('20', '21', '22')]
    with store.connection() as db:
        db.execute(f"UPDATE chat_turns SET at=(SELECT max(at) FROM chat_turns) WHERE id IN ({','.join('?'*3)})", turns)
    seen = []
    for _ in range(4):
        due(agent)
        claim = checks.claim()
        if claim:
            seen += [pair['id'] for pair in claim['pairs']]
            checks.finish(claim['id'], [], 'completed')
    assert seen == [str(turn) for turn in turns]


def test_model_input_has_no_discord_markup_or_ids(setup):
    store, agent, checks, _ = setup
    save(store, 'is <@123456789012345678> right about <#234567890123456789>?', verbatim=True, ago=60,
         reply='ask <@!345678901234567890> :<a:pog:456789012345678901>')
    save(store, 'no, see https://discord.com/channels/123/567890123456789012/678901234567890123 or 789012345678901234',
         'my bad', verbatim=True)
    pair, = checks.claim()['pairs']
    assert pair['earlier'] == {'member': 'is @someone right about #channel?', 'fri': 'ask @someone ::pog:'}
    assert pair['later']['member'] == 'no, see [discord link] or [id]'


def test_pending_batches_forget_erased_members_and_expire_without_fri(setup):
    store, agent, checks, _ = setup
    exchange(store)
    exchange(store, user='40', earlier_provenance=[ASH])
    assert len(checks.claim()['pairs']) == 2
    store.clear_chats('20')
    store.personal_control('30', 'disable')  # Someone the second pair drew on.
    with store.connection() as db:
        assert agent.get(db, 'reply_check_claim')['pairs'] == []
    agent.update({'reply_check_enabled': False}, 1, '10')  # Fri never comes back to finish it.
    with store.connection() as db:
        claim = agent.get(db, 'reply_check_claim')
        agent.put(db, 'reply_check_claim', {**claim, 'at': claim['at']-fri_reply_checks.CLAIM_TIMEOUT})
    store.status()
    with store.connection() as db:
        assert agent.get(db, 'reply_check_claim') is None
        assert db.execute('SELECT status FROM fri_reply_check_runs').fetchone()[0] == 'interrupted'


def test_a_batch_only_keeps_what_current_settings_allow(setup):
    store, agent, checks, _ = setup
    exchange(store)
    claim = checks.claim()
    agent.update({'reply_check_enabled': False}, 1, '10')
    agent.update({'reply_check_enabled': True}, 2, '10')
    with pytest.raises(ValueError):  # Restarting drops a batch claimed before it.
        flag(checks, claim)
    agent.update({'reply_check_private': True}, 3, '10')
    due(agent)
    exchange(store, conversation='private:9', gap=0)
    claim = checks.claim()
    agent.update({'reply_check_private': False}, 4, '10')
    assert flag(checks, claim)['kept'] == 0
    due(agent)
    exchange(store, user='21', gap=0)
    claim = checks.claim()
    agent.update({'enabled': False}, 5, '10')
    assert flag(checks, claim)['kept'] == 0 and findings(store) == []


def test_turns_that_vanish_before_a_retry_still_cost_their_call(setup):
    store, agent, checks, _ = setup
    exchange(store)
    claim = checks.claim()
    checks.finish(claim['id'], [], 'invalid_output')
    store.clear_chats('20')
    due(agent)
    assert checks.claim() is None
    with store.connection() as db:
        assert agent.get(db, 'reply_check_budget')['used'] == 1


def test_daily_allowance_and_interrupted_claims(setup, monkeypatch):
    store, agent, checks, _ = setup
    # Today's noon UTC, so the moves below stay inside one UTC day whenever this runs (gene #125: 601 s past 23:50
    # is tomorrow, with a fresh allowance).
    clock = time.time()//86400*86400+12*3600
    monkeypatch.setattr(fri_reply_checks.time, 'time', lambda: clock)
    agent.update({'reply_check_daily_pairs': 10}, 1, '10')
    for user in range(12):
        exchange(store, user=str(100+user))
    first = [pair['id'] for pair in checks.claim()['pairs']]
    assert len(first) == 10
    due(agent)
    assert checks.claim() is None  # One claim at a time.
    monkeypatch.setattr(fri_reply_checks.time, 'time', lambda: clock+601)
    due(agent)
    assert checks.claim() is None  # The interrupted call spent today's allowance.
    with store.connection() as db:
        run = db.execute('SELECT status,pairs FROM fri_reply_check_runs').fetchone()
        assert tuple(run) == ('interrupted', 10)
    monkeypatch.setattr(fri_reply_checks.time, 'time', lambda: clock+86400)
    due(agent)
    assert [pair['id'] for pair in checks.claim()['pairs']] == first  # Retried tomorrow.


def test_old_findings_expire_with_housekeeping_even_while_checks_are_off(setup):
    store, agent, checks, _ = setup
    exchange(store)
    flag(checks, checks.claim())
    agent.update({'reply_check_enabled': False}, 1, '10')
    with store.connection() as db:
        db.execute('UPDATE fri_reply_findings SET at=at-?', (fri_reply_checks.RETENTION+60,))
        db.execute('UPDATE fri_reply_check_runs SET at=at-?', (fri_reply_checks.RETENTION+60,))
    store.status()
    with store.connection() as db:
        assert findings(store) == [] and not db.execute('SELECT 1 FROM fri_reply_check_runs').fetchone()


def test_overview_and_export_count_everything_and_name_nobody(setup):
    store, agent, checks, settings = setup
    topics = fri_reply_checks.TOPICS[:7]
    for index, topic in enumerate(topics):
        due(agent)
        exchange(store, user=str(200+index), conversation='public:777777')
        flag(checks, checks.claim(), topic=topic, severity='low' if index else 'high')
    overview = checks.overview()
    assert (overview['flagged'], overview['checked_pairs'], overview['categories']) == (7, 7, {'wrong_info': 7})
    assert overview['severities'] == {'low': 6, 'high': 1} and len(overview['themes']) == 5
    assert overview['themes'][0] == {'category': 'wrong_info', 'topic': 'games', 'fix': 'verify_facts',
                                     'count': 1, 'score': 3, 'severities': {'high': 1},
                                     'last_day': time.strftime('%Y-%m-%d', time.gmtime())}
    exported = fri_reply_checks.export(settings.db, 7)
    assert exported['flagged'] == 7 and {theme['topic'] for theme in exported['themes']} == set(topics)
    text = json.dumps(exported)
    assert not any(value in text for value in ('777777', '"200"', 'zelda', 'delayed'))
    fri_reply_checks.purge(settings.db)
    with sqlite3.connect(settings.db) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='fri_reply_findings'").fetchone()
        config = json.loads(db.execute("SELECT value FROM agent_state WHERE key='config'").fetchone()[0])
        assert config['enabled'] and not any(key.startswith('reply_check') for key in config)
        assert not db.execute("SELECT 1 FROM agent_state WHERE key LIKE '%reply_check%'").fetchone()


def test_api_is_service_authenticated_and_overview_is_owner_only(tmp_path):
    settings = Settings(tmp_path/'api.db', 'guild', 'owner', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='workshop', fri_token='fri-'+'x'*32)
    client = TestClient(create_app(settings))
    assert 'reply-checks' in client.get('/fri/contract', headers=headers('fri')).json()['capabilities']
    assert client.post('/agent/reply-checks/claim', headers=headers('runtime')).status_code == 401
    assert client.post('/agent/reply-checks/claim', headers=headers('fri')).json() is None
    owner, member = {'guild_id': 'guild', 'user_id': 'owner'}, {'guild_id': 'guild', 'user_id': 'member'}
    assert client.post('/agent/reply-checks/overview', headers=headers('fri'), json=member).status_code == 403
    assert client.post('/agent/reply-checks/overview', headers=headers('fri'), json=owner).json()['flagged'] == 0
    body = {'findings': [], 'status': 'completed'}
    assert client.post('/agent/reply-checks/'+'a'*32+'/finish', headers=headers('fri'), json=body).status_code == 400
    assert client.post('/agent/reply-checks/nope/finish', headers=headers('fri'), json=body).status_code == 404
    usage = {'attempt_id': 'b'*32, 'model': 'gpt-6-luna', 'effort': 'low', 'status': 'completed',
             'counters': {}, 'role': 'attention', 'purpose': 'reply_check'}
    assert client.post('/chat/usage/report', headers=headers('fri'), json=usage).status_code == 200
    with sqlite3.connect(settings.db) as db:
        assert db.execute('SELECT role FROM agent_usage_roles').fetchone() == ('reply_check',)
