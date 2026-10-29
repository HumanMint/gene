import asyncio
import copy
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from gene.fri_memory_panel import EditMemory, MemoryLauncher, MemoryView, allowed, memory_component
from gene.fri_memory_store import edit, entries, save
from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_actions import harness
from tests.test_gene_controls import control_body, controls_api  # noqa: F401


def remember(store, text='I like Minecraft', category='games', user='20', scope='public:1', revision=0):
    return store.save_chat('123', scope, user, revision, text, 'noted', 'model', 'high',
                          notes=[{'category': category, 'evidence': text}])


def source():
    return {**control_body()['source'], 'kind': 'discord_interaction'}


def test_preferences_keep_several_per_category_dedupe_bound_and_survive_restart(store):
    for index in range(5):
        remember(store, 'I like game '+str(index))
    for category in ('music', 'hobbies', 'conversation', 'nickname'):
        for index in range(3):
            remember(store, 'I like '+category+' '+str(index), category)
    profile = entries(Store(store.path), '123', 'public:1', '20')
    assert len(profile['entries']) == 10
    assert all(sum(row['category'] == category for row in profile['entries']) <= 3
               for category in ('games', 'music', 'hobbies', 'conversation', 'nickname'))
    latest = profile['entries'][0]
    remember(store, latest['evidence'], latest['category'])
    assert len(entries(store, '123', 'public:1', '20')['entries']) == 10
    assert latest['expires_at']-latest['at'] == 90*86400
    assert not entries(store, '123', 'public:2', '20')['entries']
    assert len(entries(store, '123', 'private:1', '20')['entries']) == 10  # Only the member sees a DM.
    assert not entries(store, '123', 'public:1', '30')['entries']


def kept(db, scope='public:1', category='games'):
    return [row[0] for row in db.execute('SELECT evidence FROM personal_notes WHERE guild_id=? AND channel_id=? '
                                         'AND user_id=? AND category=? ORDER BY at DESC,id DESC',
                                         ('123', scope, '20', category))]


def test_the_same_words_in_other_capitals_renew_one_note_instead_of_crowding_out_another(store):
    now = time.time()
    with store.connection() as db:
        for at, evidence in enumerate(('I play league', 'I play valorant', 'i play valorant', 'I play valorant most nights')):
            assert save(db, '123', 'public:1', '20', ('games', evidence), now-100+at)
        assert kept(db) == ['I play valorant most nights', 'i play valorant', 'I play league']
        # Curly and straight apostrophes are the same words too.
        assert save(db, '123', 'public:1', '20', ('projects', 'I’m building a mod'), now-50)
        assert save(db, '123', 'public:1', '20', ('projects', "i'm building a mod"), now-40)
        assert kept(db, category='projects') == ["i'm building a mod"]


def test_folding_needs_the_same_words_not_a_shorter_or_longer_note(store):
    now = time.time()
    with store.connection() as db:
        for at, evidence in enumerate(('I play chess only on weekends', 'I play chess', 'I like coffee cake',
                                       'I like coffee')):
            assert save(db, '123', 'public:1', '20', ('hobbies', evidence), now-100+at)
        assert kept(db, category='hobbies') == ['I like coffee', 'I like coffee cake', 'I play chess']
        # Another category is another note.
        assert save(db, '123', 'public:1', '20', ('music', 'i like coffee'), now-10)
        assert kept(db, category='music') == ['i like coffee']
        assert 'I like coffee' in kept(db, category='hobbies')


def test_folding_never_crosses_origin_or_whose_words_a_note_drew_on(store):
    now = time.time()
    with store.connection() as db:
        assert save(db, '123', 'server', '20', ('games', 'I like chess'), now-30, 'public:1', [])
        assert save(db, '123', 'server', '20', ('games', 'i like chess'), now-20, 'public:2', [])
        assert save(db, '123', 'server', '20', ('games', 'I LIKE CHESS'), now-10, 'public:2',
                    [{'user_id': '30', 'revision': 0}])
        assert kept(db, 'server') == ['I LIKE CHESS', 'i like chess', 'I like chess']
        # Unknown provenance (None) isn't the same as known to be their own words ([]).
        assert save(db, '123', 'public:1', '20', ('games', 'I like go'), now-30)
        assert save(db, '123', 'public:1', '20', ('games', 'i like go'), now-20, None, [])
        assert kept(db) == ['i like go', 'I like go']
        # The same people in another order are the same provenance.
        two = [{'user_id': '30', 'revision': 1}, {'user_id': '40', 'revision': 2}]
        assert save(db, '123', 'public:1', '20', ('music', 'I like jazz'), now-30, None, two)
        assert save(db, '123', 'public:1', '20', ('music', 'i like jazz'), now-20, None, two[::-1])
        assert kept(db, category='music') == ['i like jazz']


def test_an_older_wording_never_replaces_a_newer_one(store):
    now = time.time()
    with store.connection() as db:
        assert save(db, '123', 'public:1', '20', ('games', 'i play valorant'), now)
        # A background pass replays an archive message from before it.
        assert not save(db, '123', 'public:1', '20', ('games', 'I play valorant'), now-3600)
        assert not save(db, '123', 'public:1', '20', ('games', 'I PLAY VALORANT'), now)
        row = db.execute('SELECT evidence,at FROM personal_notes').fetchall()
        assert [(r['evidence'], r['at']) for r in row] == [('i play valorant', now)]
        # Two wordings from before this change: a replay older than the exact one changes neither.
        db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at) VALUES (?,?,?,?,?,?)',
                   ('123', 'public:1', '20', 'games', 'I play valorant', now-60))
        assert not save(db, '123', 'public:1', '20', ('games', 'i play valorant'), now-30)
        assert kept(db) == ['i play valorant', 'I play valorant']
        assert save(db, '123', 'public:1', '20', ('games', 'i play valorant'), now+1)  # Newer: one note again.
        assert kept(db) == ['i play valorant']


def test_folding_is_a_renewal_not_a_removal(store):
    remember(store, 'I like Minecraft')
    with store.connection() as db:
        revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', ('20',)).fetchone()
        assert save(db, '123', 'public:1', '20', ('games', 'i like minecraft'), time.time()+1)
        assert db.execute('SELECT count(*) FROM personal_note_removals').fetchone()[0] == 0
        assert db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', ('20',)).fetchone() == revision
    assert store.chat_context('123', 'public:1', '20')['turns']  # Nothing derived is cleared.
    # After a deliberate removal, other capitals of the removed words are still fenced.
    note = entries(store, '123', 'public:1', '20')['entries'][0]
    edit(store, '123', 'public:1', '20', note['id'], 0, source(), 'remove')
    with store.connection() as db:
        assert not save(db, '123', 'public:1', '20', ('games', 'I LIKE MINECRAFT'), time.time()-1)
        assert not kept(db)


def test_legacy_note_migration_preserves_identity_evidence_and_time(tmp_path):
    path = tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE personal_notes (id INTEGER PRIMARY KEY,guild_id TEXT,channel_id TEXT,user_id TEXT,'
            'category TEXT,evidence TEXT,at REAL,UNIQUE(guild_id,channel_id,user_id,category))')
        db.execute('INSERT INTO personal_notes VALUES (7,?,?,?,?,?,?)',
                   ('123', 'public:1', '20', 'games', 'I like chess', time.time()))
    store = Store(path)
    assert entries(store, '123', 'public:1', '20')['entries'][0]['id'] == 7
    remember(store)
    assert len(entries(Store(path), '123', 'public:1', '20')['entries']) == 2


def test_remove_preserves_other_items_blocks_old_learning_and_fences_inflight(store):
    remember(store)
    remember(store, 'I like Terraria')
    state = entries(store, '123', 'public:1', '20')
    minecraft = next(row for row in state['entries'] if row['evidence'] == 'I like Minecraft')
    old_time = minecraft['at']
    result = edit(store, '123', 'public:1', '20', minecraft['id'], 0, source(), 'remove')
    assert [row['evidence'] for row in result['entries']] == ['I like Terraria']
    assert result['revision'] == 1
    assert not store.chat_context('123', 'public:1', '20')['turns']
    with store.connection() as db:
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft'), old_time)
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft modpacks'), old_time)
    with pytest.raises(Conflict):
        remember(store, 'I like stale responses')
    with pytest.raises(ValueError, match='changed'):
        edit(store, '123', 'public:1', '20', minecraft['id'], 0, source(), 'remove')
    # A new explicit disclosure may renew the preference after a deliberate removal.
    remember(store, revision=1)
    assert len(entries(store, '123', 'public:1', '20')['entries']) == 2
    store.personal_control('20', 'disable')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM personal_note_removals').fetchone()[0] == 0
    assert not entries(store, '123', 'public:1', '20')['entries']


def test_replace_is_explicit_and_does_not_remove_other_scopes(store):
    remember(store)
    remember(store, 'I like private chess', scope='private:1')
    note = entries(store, '123', 'public:1', '20')['entries'][0]
    result = edit(store, '123', 'public:1', '20', note['id'], 0, source(), 'replace', 'I prefer Terraria')
    assert result['entries'][0]['evidence'] == 'I prefer Terraria'
    assert entries(store, '123', 'private:1', '20')['entries'][0]['evidence'] == 'I like private chess'
    with store.connection() as db:
        assert not save(db, '123', 'public:1', '20', ('games', 'I like Minecraft'), note['at'])


@pytest.mark.parametrize('change', ['user', 'conversation', 'source_user', 'source_channel', 'source_guild', 'stale', 'expired', 'sensitive', 'quoted'])
def test_item_edits_fail_closed_at_scope_source_age_and_content_boundaries(store, change):
    remember(store)
    note = entries(store, '123', 'public:1', '20')['entries'][0]
    user, scope, event, text = '20', 'public:1', source(), 'I like chess'
    if change == 'user':
        user = event['user_id'] = '30'
    elif change == 'conversation':
        scope, event['channel_id'] = 'public:2', '2'
    elif change.startswith('source_'):
        event[change[7:]+'_id'] = '999'
    elif change == 'stale':
        event['id'] = str(int(event['id'])-(301000 << 22))
    elif change == 'expired':
        with store.connection() as db:
            db.execute('UPDATE personal_notes SET at=?', (time.time()-91*86400,))
    elif change == 'sensitive':
        text = 'I like sharing my password'
    elif change == 'quoted':
        text = '"I like chess"'
    with pytest.raises(ValueError):
        edit(store, '123', scope, user, note['id'], 0, event, 'replace', text)


def test_memory_api_requires_fri_scope_fresh_actor_and_revision(controls_api):  # noqa: F811
    client, controls = controls_api
    remember(controls.store)
    scope = {key: control_body()[key] for key in ('guild_id', 'channel_id', 'user_id', 'conversation_id')}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/chat/memory/notes', headers=headers(role), json=scope).status_code == 401
    state = client.post('/chat/memory/notes', headers=headers('fri'), json=scope).json()
    identifier = state['entries'][0]['id']
    body = {**scope, 'revision': 0, 'source': source(), 'action': 'remove'}
    path = '/chat/memory/notes/'+str(identifier)
    assert client.post(path, headers=headers('runtime'), json=body).status_code == 401
    assert client.post(path, headers=headers('fri'), json={**body, 'source': {**source(), 'user_id': '30'}}).status_code == 400
    assert client.post(path, headers=headers('fri'), json=body).status_code == 200
    assert client.post(path, headers=headers('fri'), json=body).status_code == 400


def test_memory_controls_bind_member_audience_and_modal_and_send_exact_evidence(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, channels, _ = harness(monkeypatch, tmp_path)
        fri._connection.user = SimpleNamespace(id=99)
        state = {'enabled': True, 'revision': 2, 'entries': [
            {'id': 7, 'category': 'games', 'evidence': 'I like chess', 'expires_at': time.time()+90*86400}]}
        scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        interaction = Mock(spec=discord.Interaction, id=int(source()['id']), guild_id=123, channel_id=1,
            channel=channels[1], user=SimpleNamespace(id=20, display_name='friend'),
            message=SimpleNamespace(author=fri.user), response=SimpleNamespace(send_modal=AsyncMock()))
        launcher = MemoryLauncher(fri, scope)
        view = MemoryView(fri, scope, state, selected=7)
        try:
            assert await launcher.interaction_check(interaction)
            wrong = copy.copy(interaction)
            wrong.user = SimpleNamespace(id=30)
            assert not await view.interaction_check(wrong)
            wrong = copy.copy(interaction)
            wrong.channel_id = 2
            assert not await view.interaction_check(wrong)
            await view.replace(interaction)
            modal = interaction.response.send_modal.await_args.args[0]
            assert isinstance(modal, EditMemory)
            modal.preference._value = 'I prefer Terraria'
            fri.api = AsyncMock(return_value={**state, 'revision': 3})
            agent.forget = Mock()
            with patch('gene.fri_memory_panel.show', new=AsyncMock()) as show:
                await modal.on_submit(interaction)
            body = fri.api.await_args.args[2]
            assert body['evidence'] == 'I prefer Terraria' and body['revision'] == 2
            assert body['source']['user_id'] == '20' and body['conversation_id'] == 'public:1'
            agent.forget.assert_called_once_with(20)
            show.assert_awaited_once()
            agent.member.return_value = None
            assert not await allowed(fri, interaction, scope)
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_dm_memory_dispatch_only_admits_our_components_and_bound_modal():
    interaction = SimpleNamespace(guild_id=None, type=SimpleNamespace(value=3),
        message=SimpleNamespace(author=SimpleNamespace(id=99)),
        data={'custom_id': 'fri:memory:open:'+'a'*32})
    assert memory_component(interaction, 99)
    interaction.message.author.id = 30
    assert not memory_component(interaction, 99)
    interaction.type.value = 5
    interaction.data['custom_id'] = 'fri:memory:edit:'+'a'*32
    assert memory_component(interaction, 99)  # The registered modal independently binds its member and scope.
    interaction.data['custom_id'] = 'other:memory:edit:'+'a'*32
    assert not memory_component(interaction, 99)
