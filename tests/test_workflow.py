from concurrent.futures import ThreadPoolExecutor

import pytest

from gene.store import Conflict, Store
from tests.conftest import proposed, request


def test_complete_review_deploy_rollback_flow(store):
    job = proposed(store)
    assert store.claim_deploy() is None
    store.approve(job['id'], 'b' * 40, 'owner')
    release = store.claim_deploy()
    assert release['approved_by'] == 'owner'
    assert store.claim_deploy() is None
    store.finish_deploy(release['id'], release['lease'], True, 'Healthy')
    assert store.status()['active_sha'] == 'b' * 40
    rollback = store.rollback('guild', 'owner')
    claim = store.claim_deploy()
    assert claim['id'] == rollback['id']
    store.finish_deploy(claim['id'], claim['lease'], True, 'Previous version healthy')
    assert store.status()['active_sha'] == 'a' * 40


def test_failed_release_does_not_advance_active_version(store):
    job = proposed(store)
    store.approve(job['id'], 'b' * 40, 'owner')
    release = store.claim_deploy()
    store.finish_deploy(release['id'], release['lease'], False, 'Unhealthy; previous version restored')
    assert store.status()['active_sha'] == 'a' * 40
    assert store.get(job['id'])['state'] == 'failed'


def test_request_and_usage_are_idempotent(store):
    first = request(store)
    assert request(store)['id'] == first['id']
    job = store.claim()
    counters = {'input_tokens': 20, 'cached_input_tokens': 12, 'output_tokens': 4, 'turns': 1}
    for _ in range(3):
        store.add_usage(job['id'], job['lease'], 'attempt', 'model', counters)
    row = store.usage()[0]
    assert row['attempts'] == 1
    assert row['input_tokens'] == 20
    assert row['cached_input_tokens'] == 12


def test_unknown_usage_stays_unknown(store):
    request(store)
    job = store.claim()
    store.add_usage(job['id'], job['lease'], 'attempt', 'model', {})
    row = store.usage()[0]
    assert row['input_tokens'] is None
    assert row['output_tokens'] is None
    assert row['unknown_attempts'] == 1


def test_only_one_worker_can_claim_concurrently(store):
    request(store)
    request(store, source='message-2')
    with ThreadPoolExecutor(4) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(4)))
    assert sum(j is not None for j in claims) == 1


def test_approval_requires_full_current_tested_commit(store):
    job = proposed(store)
    for sha in ('b' * 7, 'c' * 40):
        with pytest.raises(Conflict):
            store.approve(job['id'], sha, 'owner')
    assert store.get(job['id'])['state'] == 'review'


def test_other_deployment_invalidates_old_proposal(store):
    first = proposed(store, source='first')
    second = proposed(store, sha='c' * 40, source='second')
    store.approve(first['id'], 'b' * 40, 'owner')
    store.approve(second['id'], 'c' * 40, 'owner')
    release = store.claim_deploy()
    store.finish_deploy(release['id'], release['lease'], True, 'Healthy')
    assert store.claim_deploy() is None
    assert store.get(second['id'])['state'] == 'failed'


def test_stale_worker_cannot_finish_or_add_usage(store):
    request(store)
    job = store.claim()
    with pytest.raises(Conflict):
        store.finish_build(job['id'], 'wrong', None)
    with pytest.raises(Conflict):
        store.add_usage(job['id'], 'wrong', 'attempt', 'model', {})
    store.finish_build(job['id'], job['lease'], None, 'Failed')
    with pytest.raises(Conflict):
        store.finish_build(job['id'], job['lease'], None)


def test_pause_keeps_bot_state_but_stops_new_work(store):
    request(store)
    store.set_paused(True, 'owner')
    assert store.claim() is None
    assert store.status()['active_sha'] == 'a' * 40
    store.set_paused(False, 'owner')
    assert store.claim() is not None


def test_restart_and_backup_preserve_requests_context_and_release(store, tmp_path):
    job = request(store)
    memory = store.remember('guild', 'owner', 'Music is welcome in the lounge.', 'https://discord.com/x')
    fresh = Store(store.path)
    assert fresh.get(job['id'])['state'] == 'queued'
    assert fresh.memories('guild')[0]['id'] == memory
    fresh.backup(tmp_path / 'backup.sqlite3')
    backup = Store(tmp_path / 'backup.sqlite3')
    assert backup.status()['active_sha'] == 'a' * 40
    fresh.forget(memory, 'guild', 'owner')
    assert fresh.memories('guild') == []
    assert len(backup.memories('guild')) == 1


def test_request_minimizes_secrets(store):
    job = store.request('guild', 'member', 'secret', '', 'Please use token=verysecretvalue for this')
    assert 'verysecretvalue' not in job['body']
    assert '[REDACTED]' in job['body']


def test_deployment_lease_survives_restart_for_operator_reconciliation(store):
    job = proposed(store)
    store.approve(job['id'], 'b' * 40, 'owner')
    deployed = store.claim_deploy()
    fresh = Store(store.path)
    assert fresh.claim_deploy() is None
    assert fresh.get(deployed['id'])['state'] == 'deploying'

