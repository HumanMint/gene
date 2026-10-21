import copy
import time

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.repair_policy import ACTOR, POLICY, REVIEW_EFFORT, REVIEW_MODEL, regression_path
from gene.repair_store import request
from gene.store import Store
from tests.conftest import proposed


def payload(user='20', channel='1', guild='123', offset=0):
    stamp=int((time.time()+offset)*1000-1420070400000)<<22
    return {'guild_id': guild, 'user_id': user, 'channel_id': channel,
        'conversation_id': 'public:'+channel, 'revision': 0,
        'source': {'kind':'discord_message', 'id':str(stamp), 'user_id':user,
                   'display_name':'Member', 'channel_id':channel, 'guild_id':guild},
        'body':'The existing vote counter counts my self-vote.'}


def enable(store):
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key="repair_policy"', (POLICY,))


def proposal(job):
    verdict={'decision':'allow', 'reason':'The existing score contract excludes self votes; this restores that behavior.',
             'model': REVIEW_MODEL, 'effort': REVIEW_EFFORT}
    return {'sha':'b'*40,'base_sha':job['base_sha'], 'branch':'codex/repair-test', 'tests_passed':True,
        'summary':'Restore the existing self-vote rule.', 'repair_verification': {
            'policy':POLICY,'sha':'b'*40,'base_sha':job['base_sha'],'request_ref':job['repair']['request_ref'],
            'baseline_passed':True,'base_failed':True,'candidate_passed':True,
            'regression_path':regression_path(job['id']), 'regression_hash':'c'*64,
            'verification':verdict, 'review':copy.deepcopy(verdict)}}


def queued(store):
    enable(store)
    result=request(store,payload())
    assert result['accepted']
    job=store.claim()
    assert job['kind']=='repair' and job['intake_approved_by'] is None
    return job


def test_verified_repair_can_build_and_deploy_under_standing_policy(store):
    job=queued(store)
    store.finish_build(job['id'],job['lease'],proposal(job))
    saved=store.get(job['id'])
    assert saved['state']=='approved' and saved['approved_by']==ACTOR and saved['approved_sha']=='b'*40
    claimed=store.claim_deploy()
    assert claimed['id']==job['id']
    store.finish_deploy(claimed['id'],claimed['lease'],True,'Repair release checks passed.')
    assert store.status()['active_sha']=='b'*40
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM audit WHERE actor="owner" AND target=?',(job['id'],)).fetchone()[0]==0


@pytest.mark.parametrize('field,value', [('base_failed',False),('base_failed','true'),('candidate_passed',False),
    ('baseline_passed',False),('sha','d'*40),('base_sha','d'*40),('policy','anything'),
    ('request_ref','a'*64),('regression_hash',''),('regression_path','tests/test_api.py')])
def test_incomplete_or_mismatched_evidence_never_auto_releases(store,field,value):
    job=queued(store)
    candidate=proposal(job)
    candidate['repair_verification'][field]=value
    store.finish_build(job['id'],job['lease'],candidate)
    assert store.get(job['id'])['state']=='review'
    assert store.get(job['id'])['kind']=='feature'
    assert store.claim_deploy() is None


@pytest.mark.parametrize('phase',['verification','review'])
def test_independent_denial_routes_to_owner_exact_sha_review(store,phase):
    job=queued(store)
    candidate=proposal(job)
    candidate['repair_verification'][phase]['decision']='manual'
    store.finish_build(job['id'],job['lease'],candidate)
    assert store.get(job['id'])['approved_sha'] is None
    store.approve(job['id'],'b'*40,'owner')
    assert store.claim_deploy()['approved_by']=='owner'


def test_new_feature_cannot_self_label_its_way_to_automatic_release(store):
    enable(store)
    job=proposed(store)
    assert job['kind']=='feature' and job['state']=='review'
    assert store.claim_deploy() is None


@pytest.mark.parametrize('stage',['before_claim','before_finish','before_deploy'])
@pytest.mark.parametrize('change',['base','policy'])
def test_stale_or_revoked_authority_falls_back_to_owner(store,stage,change):
    enable(store)
    result=request(store,payload())
    job=store.claim() if stage!='before_claim' else None
    if stage=='before_deploy':
        store.finish_build(job['id'],job['lease'],proposal(job))
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key=?',
                   ('d'*40 if change=='base' else 'disabled','active_sha' if change=='base' else 'repair_policy'))
    if stage=='before_claim':
        assert store.claim() is None
    elif stage=='before_finish':
        store.finish_build(job['id'],job['lease'],proposal(job))
    else:
        assert store.claim_deploy() is None
    saved=store.get(result['id'])
    assert saved['kind']=='feature' and saved['state'] in ('review','awaiting_owner')
    assert saved['approved_sha'] is None


def test_unverified_report_needs_owner_before_normal_coding(store):
    job=queued(store)
    store.finish_build(job['id'],job['lease'],None,'Cannot reproduce an existing feature defect.')
    saved=store.get(job['id'])
    assert saved['state']=='awaiting_owner' and saved['kind']=='feature'
    assert store.claim() is None
    notice=store.claim_review_notice()
    assert notice['phase']=='intake'


def test_restart_deduplication_budget_pause_and_owner_notice(store):
    enable(store)
    first=request(store,payload())
    assert Store(store.path).get(first['id'])['state']=='queued'
    assert request(store,payload(user='21'))['id']==first['id']
    assert store.claim_review_notice()['phase']=='status'
    for index in range(3):
        job=store.claim()
        store.finish_build(job['id'],job['lease'],None,'Unverified report; needs owner review.')
        if index<2:
            # New source events are bounded even after unsuccessful attempts.
            next_body=payload(user=str(22+index))
            next_body['source']['id']=str(int(next_body['source']['id'])+index+100)
            assert request(store,next_body)['accepted']
    assert not request(store,payload(user='29'))['accepted']
    store.set_paused(True,'owner')
    assert 'paused' in request(store,payload(user='30'))['reason']


def test_disabled_and_erased_or_stale_messages_do_not_start_repairs(store):
    assert not request(store,payload())['accepted']
    enable(store)
    with pytest.raises(ValueError,match='fresh'):
        request(store,payload(offset=-400))
    with store.connection() as db:
        db.execute('INSERT INTO chat_revisions VALUES (?,?)',('20',1))
    with pytest.raises(ValueError,match='cleared'):
        request(store,payload())


def test_erasure_cancels_a_running_automatic_repair(store):
    job=queued(store)
    store.erase_user('20')
    assert store.get(job['id'])['state']=='rejected'
    with pytest.raises(ValueError):
        store.finish_build(job['id'],job['lease'],proposal(job))
    assert store.claim_deploy() is None


def test_api_uses_fri_credential_scope_and_source_provenance(tmp_path):
    settings=Settings(tmp_path/'gene.db','123','owner','r'*32,'w'*32,'d'*32,
        workshop_channel_id='1', fri_token='f'*32)
    with TestClient(create_app(settings)) as client:
        store=client.app.state.store
        store.bootstrap('a'*40)
        enable(store)
        store.set_paused(False,'owner')
        for role in 'rwd':
            assert client.post('/repairs',headers={'Authorization':'Bearer '+role*32},json=payload()).status_code==401
        headers={'Authorization':'Bearer '+'f'*32}
        assert client.post('/repairs',headers=headers,json=payload(guild='999')).status_code==403
        assert client.post('/repairs',headers=headers,json=payload(channel='9')).status_code==403
        forged=payload()
        forged['source']['user_id']='owner'
        assert client.post('/repairs',headers=headers,json=forged).status_code==400
        forged=payload()
        forged['approved']=True
        assert client.post('/repairs',headers=headers,json=forged).status_code==422
        result=client.post('/repairs',headers=headers,json=payload())
        assert result.status_code==200 and result.json()['accepted']
        scope={k:v for k,v in payload().items() if k in ('guild_id','user_id','channel_id','conversation_id')}
        assert len(client.post('/repairs/recent',headers=headers,json=scope).json()['repairs'])==1
        scope['user_id']='other'
        assert client.post('/repairs/recent',headers=headers,json=scope).json()['repairs']==[]
