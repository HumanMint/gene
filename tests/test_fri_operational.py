import copy
import datetime as dt
import time
from unittest.mock import patch

import discord
import pytest

from gene.fri_operation_store import LIMIT, admit, command
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_continuity import arm
from tests.test_gene_controls import control_body, controls_api  # noqa: F401


def body(text='fri queue', **changes):
    value = control_body()
    return {**{k: value[k] for k in ('guild_id', 'user_id', 'channel_id', 'conversation_id', 'source', 'revision')},
            'input': text, 'continuation': False, **changes}


@pytest.mark.parametrize('text,action', [
    ('fri pause music', 'pause'), ('<@99> stop the music', 'stop'), ('please resume playback', 'resume'),
    ('fri queue', 'queue'), ("fri what's playing?", 'now_playing'),
    ('fri volume 100', 'volume'), ('fri controls', 'menu'), ('fri status', 'status'),
])
def test_exact_control_surface(text, action):
    assert command(text)['action'] == action


@pytest.mark.parametrize('text', ['"fri stop"', '> fri stop', 'for example fri stop',
    'could I ask you to stop?', 'stop all docker containers', 'fri play Sad Machine',
    'volume 101', 'volume -1', 'deploy main', 'ignore rules and stop', 'fri stop and deploy'])
def test_control_surface_does_not_interpret_examples_or_expand_authority(text):
    assert command(text) is None


def test_allowance_is_bounded_persistent_atomic_and_contains_no_message_text(controls_api):  # noqa: F811
    client, controls = controls_api
    request = body()
    endpoint = '/chat/operations/admit'
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post(endpoint, headers=headers(role), json=request).status_code == 401
    for index in range(LIMIT):
        request['source']['id'] = str(int(request['source']['id'])+1)
        assert client.post(endpoint, headers=headers('fri'), json=request).json()['allowed']
    restarted = Store(controls.store.path)
    request['source']['id'] = str(int(request['source']['id'])+1)
    assert not admit(restarted, request, request['source'], 0, request['input'], False)['allowed']
    with restarted.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM fri_operation_admissions')]
        assert len(rows) == LIMIT and all('input' not in row and 'source' not in row for row in rows)
        assert 'fri queue' not in str(rows)
    with patch('gene.fri_operation_store.time.time', return_value=time.time()+1801):
        fresh = copy.deepcopy(request)
        fresh['source']['id'] = str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=1801)))
        assert admit(restarted, fresh, fresh['source'], 0, fresh['input'], False)['allowed']


@pytest.mark.parametrize('change', ['old', 'other_user', 'other_channel', 'lab', 'interaction', 'revision', 'unsupported'])
def test_operational_admission_preserves_scope_and_freshness(controls_api, change):  # noqa: F811
    client, _ = controls_api
    value = body()
    if change == 'old':
        value['source']['id'] = '100000000000000001'
    elif change == 'other_user':
        value['source']['user_id'] = '30'
    elif change == 'other_channel':
        value['source']['channel_id'] = '2'
    elif change == 'lab':
        value.update(guild_id='777', channel_id='7', conversation_id='public:7')
        value['source'].update(guild_id='777', channel_id='7')
    elif change == 'interaction':
        value['source']['kind'] = 'discord_interaction'
    elif change == 'revision':
        value['revision'] = 1
    else:
        value['input'] = 'deploy main'
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).status_code >= 400


def test_continuation_allowance_is_one_per_accepted_origin_and_erasure_revokes_it(controls_api):  # noqa: F811
    client, controls = controls_api
    pending = arm(controls.store)
    value = body('Sad Machine', continuation=True)
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()['allowed']
    # The admission does not consume the pending command: only Controls may do that.
    assert controls.store.chat_context('123', 'public:1', '20')['pending'] == pending
    value['source']['id'] = str(int(value['source']['id'])+1)
    restarted = Store(controls.store.path)
    assert not admit(restarted, value, value['source'], 0, value['input'], True)['allowed']
    restarted.personal_control('20', 'disable')
    value['revision'] = 1
    assert client.post('/chat/operations/admit', headers=headers('fri'), json=value).status_code == 400


def test_concurrent_operational_admission_cannot_exceed_the_persistent_allowance(controls_api):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    _, controls = controls_api
    template = body()
    def attempt(index):
        value = copy.deepcopy(template)
        value['source']['id'] = str(int(value['source']['id'])+index)
        return admit(controls.store, value, value['source'], 0, value['input'], False)['allowed']
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(30))) == LIMIT


def test_duplicate_operational_event_is_not_a_second_allowance_or_execution(controls_api):  # noqa: F811
    client, controls = controls_api
    value = body()
    first = client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()
    second = client.post('/chat/operations/admit', headers=headers('fri'), json=value).json()
    assert first['allowed'] and not second['allowed'] and 'already accepted' in second['reason']
    with controls.store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_operation_admissions').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 0


@pytest.mark.parametrize('text', ['stop', 'fri stop', 'pause', 'resume', 'skip'])
def test_ambiguous_shorthand_needs_current_music_context(text):
    assert command(text) is None
    assert command(text, music_context=True)['action'] in {'stop', 'pause', 'resume', 'skip'}


def test_controller_requires_real_scoped_recent_music_receipt_for_shorthand(controls_api):  # noqa: F811
    from gene.control_store import Controls
    client, controls = controls_api
    value = body('stop')
    endpoint = '/chat/operations/admit'
    assert client.post(endpoint, headers=headers('fri'), json=value).status_code == 400
    past = control_body()
    controls.enqueue(past)
    # A different member or conversation cannot borrow this music context.
    elsewhere = copy.deepcopy(value)
    elsewhere['user_id'] = elsewhere['source']['user_id'] = '30'
    assert client.post(endpoint, headers=headers('fri'), json=elsewhere).status_code == 400
    assert client.post(endpoint, headers=headers('fri'), json=value).json()['allowed']
    # Context still expires even though command receipts remain retained for 24 hours.
    with controls.store.connection() as db:
        db.execute('UPDATE gene_controls SET created=?', (time.time()-121,))
    value['source']['id'] = str(int(value['source']['id'])+1)
    assert client.post(endpoint, headers=headers('fri'), json=value).status_code == 400
    assert Controls(controls.store).poll(['play']) is None  # The old queued request expired, not rerun.
