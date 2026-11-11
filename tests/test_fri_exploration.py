from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_agent_store import AgentStore, snowflake_floor
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_learning import learning as learning


def archive(learning):
    clock, store, settings, _ = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = AgentStore(store, settings)
    scheduler.update({'exploration_enabled': True, 'exploration_days': 365}, 1, '10')
    return clock, store, settings, scheduler


def assigned(scheduler, claim):
    return scheduler.exploration.target(claim['id'], [{'user_id': '11', 'name': 'friend', 'role_id': '90'}])


def row(at, user='11', text='played minecraft with everyone', channel='1', bot=False):
    return {'id': str(int(snowflake_floor(at))+1), 'channel_id': channel,
            'author': {'id': user, 'bot': bot}, 'content': text}


def raw_record(item):
    import discord
    return {'id': item['id'], 'user_id': item['author']['id'], 'display_name': 'friend',
            'role_id': '90', 'revision': 0, 'text': item['content'],
            'at': discord.utils.snowflake_time(int(item['id'])).timestamp()}


def note(item, **changes):
    return {'source_id': item['id'], 'category': 'games', 'topic': 'Minecraft',
            'evidence': item['content'], **changes}


def finish(scheduler, claim, items, observations=(), **changes):
    return scheduler.finish(claim['id'], [raw_record(r) for r in items],
        {'summary': 'Minecraft discussion', 'observations': list(observations)}, len(items),
        min((r['id'] for r in items), default=None), True, {},
        scanned_ids=[r['id'] for r in items], **changes)


def test_plans_are_bound_to_claim_members_channels_and_privacy(learning):
    _, store, settings, scheduler = archive(learning)
    claim = assigned(scheduler, scheduler.claim())
    client = TestClient(create_app(settings))
    payload = {'member_id': '11', 'term': 'minecraft', 'reason': 'learn a shared game'}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/agent/learning/'+claim['id']+'/plan', headers=headers(role), json=payload).status_code == 401
    for member, term in [('999', ''), ('', 'password'), ('', 'https://example.com'), ('', 'a'*81)]:
        with pytest.raises(ValueError):
            scheduler.exploration.prepare(claim['id'], member, term, 'read history')
    plan = scheduler.exploration.prepare(claim['id'], **payload)
    assert plan['before_id'] == claim['until_id']
    with pytest.raises(ValueError, match='already'):
        scheduler.exploration.prepare(claim['id'], **payload)
    store.personal_control('11', 'disable')
    with pytest.raises(ValueError):
        scheduler.exploration.prepare(claim['id'], **payload)


def test_failed_inference_does_not_check_off_history_and_erasure_blocks_inflight_notes(learning):
    clock, store, settings, scheduler = archive(learning)
    item = row(clock[0]-100*86400)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', 'minecraft', 'discover who discusses the game')
    finish(scheduler, claim, [item], [note(item)], error='Summary failed')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_explore_queries').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_notes').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_seen').fetchone()[0] == 0
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    scheduler = AgentStore(Store(settings.db), settings)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', 'minecraft', 'try the unread archive')
    assert finish(scheduler, claim, [item], [note(item)])['historical_notes'] == 1
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'look at another conversation')
    store.personal_control('11', 'disable')
    result = finish(scheduler, claim, [item], [note(item)])
    assert result['error'] and result['historical_notes'] == 0
    assert 'Minecraft' not in scheduler.reports()[0]['body']
    assert not scheduler.context('1', '11')['historical_topics']
    assert not scheduler.overview()['historical_notes']
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_explore_seen').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_members').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fri_explore_queries').fetchone()[0] == 0


def test_source_forgery_and_sensitive_candidates_do_not_become_member_notes(learning):
    clock, _, _, scheduler = archive(learning)
    item = row(clock[0]-100*86400)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'discover past conversations')
    findings = [note(item, source_id='999'), note(item, evidence='made up quote'),
                note(item, topic='health diagnosis'), note(item)]
    assert finish(scheduler, claim, [item], findings)['historical_notes'] == 1
    assert scheduler.context('1', '11')['historical_topics'][0]['evidence'] == item['content']
    # A short nonempty search page remains unfinished, unlike ordinary history pagination.
    scheduler.delivered(claim['id'])
    clock[0] += 1800
    next_claim = assigned(scheduler, scheduler.claim())
    assert scheduler.exploration.prepare(next_claim['id'], '11', '', 'continue older history')['before_id'] == item['id']


INTERRUPTED = {'blocked': 'The previous pass was interrupted. Its report is pending; its allowance remains reserved.'}


@pytest.mark.parametrize('value', [None, 'oops', 10**400], ids=['null', 'text', 'huge integer'])
def test_a_claim_time_that_cant_be_read_is_refused_by_target_not_crashed(learning, value):
    """gene #184: with an unassigned exploration claim and a completed query for the member, target() compared the
    claim's stored time unchecked and raised TypeError. It's refused like any claim it can't use, and the next pass
    reports it interrupted, as claim() treats a claim whose time can't be read."""
    clock, store, settings, scheduler = archive(learning)
    claim = scheduler.claim()
    with store.connection() as db:
        db.execute('INSERT INTO fri_explore_queries(key,channel_id,user_id,term,depth,before_id,done,at,passes) '
                   'VALUES (?,?,?,?,?,?,1,?,1)', (scheduler.exploration.query_key('1', '11', '', claim['depth']),
                                                   '1', '11', '', claim['depth'], '0', clock[0]-30*86400))
        stored = AgentStore.get(db, 'learning_claim')
        stored['at'] = value
        AgentStore.put(db, 'learning_claim', stored)
    with pytest.raises(ValueError, match='No unassigned archive exploration claim'):
        assigned(scheduler, claim)
    assert scheduler.claim(manual=True) == INTERRUPTED
