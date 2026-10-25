"""One profile per member across the server's readable channels; private conversations stay put."""

import sqlite3
import time

import pytest

from gene.fri_agent_store import RANGES
from gene.fri_memory import TTL
from gene.fri_memory_store import HORIZON, digest, edit, entries, fences, removed, save
from gene.fri_protocol import compatible, contract
from gene.profile_downgrade import main as downgrade
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_agent import finish as learn
from tests.test_fri_endings import source
from tests.test_fri_memory_items import source as control_source
from tests.test_fri_mention_api import h, invited, post  # noqa: F401
from tests.test_fri_mention_api import source as mention_source
from tests.test_fri_mention_api import turn as mention_turn
from tests.test_gene_controls import control_body, controls_api  # noqa: F401

ASH = {'user_id': '30', 'revision': 0}


def say(store, text='I like Minecraft', category='games', conversation='public:1', audience='server', user='20',
        reply='noted', provenance=(), revision=0, **extra):
    return store.save_chat('123', conversation, user, revision, text, reply, 'model', 'high',
                           notes=[{'category': category, 'evidence': text}] if category else [],
                           source=source(user=user, channel=conversation.split(':')[1]),
                           provenance=list(provenance), audience=audience, **extra)


def notes(store, conversation, user='20', profiles=True):
    return [(note['scope'], note['evidence']) for note in
            store.chat_context('123', conversation, user, profiles)['personal_memory']['notes']]


def elsewhere(store, conversation, user='20'):
    return [(turn['conversation_id'], turn['input']) for turn in store.chat_context('123', conversation, user)['elsewhere']]


def older_controller_saves(db, channel, note, at, user='20'):
    """The removal check a controller without member profiles runs before it saves a note."""
    cutoff = db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND channel_id=? AND user_id=? '
                        'AND digest IN (?,?)', ('123', channel, user, digest(*note), digest(note[0], ''))).fetchone()[0]
    return cutoff is None or at > cutoff


def learner(store, tmp_path):
    from gene.config import Settings
    from gene.fri_agent_store import AgentStore
    agent = AgentStore(store, Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40,
                                       agent_channels='2', agent_history_channels='2', agent_role_id='admin'))
    agent.update({'enabled': True}, 0, 'owner')
    return agent


def test_contract_advertises_member_profiles(client):
    assert 'member-profiles' in client.get('/fri/contract', headers=headers('fri')).json()['capabilities']


def test_profile_notes_follow_the_member_and_conversation_notes_stay(store):
    say(store)
    say(store, 'I like restricted chess', conversation='public:5', audience='conversation')
    say(store, 'I like private jazz', 'music', conversation='private:9', audience='conversation')
    assert notes(store, 'public:2') == [('server', 'I like Minecraft')]
    assert notes(store, 'public:5') == [('server', 'I like Minecraft'), ('here', 'I like restricted chess')]
    assert notes(store, 'private:9') == [('server', 'I like Minecraft'), ('here', 'I like private jazz')]
    assert notes(store, 'public:1') == [('server', 'I like Minecraft')]
    assert notes(store, 'public:2', user='30') == []
    with store.connection() as db:
        assert db.execute("SELECT origin FROM personal_notes WHERE channel_id='server'").fetchone()[0] == 'public:1'


@pytest.mark.parametrize('conversation', ['private:9', 'private:1'])
def test_a_private_conversation_can_never_reach_the_profile(store, conversation):
    with pytest.raises(ValueError, match='profile'):
        say(store, conversation=conversation)
    with pytest.raises(ValueError, match='profile'):
        say(store, audience='everyone')
    assert notes(store, 'public:2') == []


def test_turn_api_refuses_server_audience_for_private_and_invited_turns(controls_api):  # noqa: F811
    client, _ = controls_api
    body = {**{key: control_body()[key] for key in ('guild_id', 'channel_id', 'user_id', 'conversation_id', 'source')},
            'revision': 0, 'input': 'I like chess', 'reply': 'ok', 'model': 'm', 'effort': 'high', 'provenance': []}
    for path in ('/chat/turn/check', '/chat/turn'):
        assert client.post(path, headers=headers('fri'), json={
            **body, 'conversation_id': 'private:1', 'audience': 'server'}).status_code == 422
        assert client.post(path, headers=headers('fri'), json={
            **body, 'mention_grant': 'a'*64, 'audience': 'server'}).status_code == 422
    assert client.post('/chat/turn', headers=headers('fri'), json={
        **body, 'audience': 'server', 'notes': [{'category': 'games', 'evidence': 'I like chess'}]}).json() == {
        'ok': True, 'first_memory': True, 'memory_scope': 'server'}
    # An older Fri sends no audience: the turn and its notes stay in the conversation.
    assert client.post('/chat/turn', headers=headers('fri'), json={
        **body, 'input': 'I like go', 'notes': [{'category': 'games', 'evidence': 'I like go'}]}).json() == {
        'ok': True, 'first_memory': True, 'memory_scope': 'conversation'}


def test_profile_keeps_three_per_category_and_fifteen_in_all(store):
    for category in ('games', 'music', 'hobbies', 'conversation', 'nickname'):
        for index in range(4):
            say(store, f'I like {category} {index}', category)
    profile = notes(store, 'public:2')
    assert len(profile) == 15
    assert ('server', 'I like games 0') not in profile and ('server', 'I like games 3') in profile


def test_first_memory_is_per_scope(store):
    assert say(store)['first_memory']
    assert not say(store, 'I like Terraria')['first_memory']
    assert say(store, 'I like chess', conversation='public:5', audience='conversation') == {
        'ok': True, 'first_memory': True, 'memory_scope': 'conversation'}
    assert not say(store, 'nothing to keep', None)['first_memory']


def test_elsewhere_is_the_latest_readable_exchanges_from_other_conversations(store):
    say(store, 'old public thing', None, conversation='public:2')
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=?', (time.time()-13*3600,))
    say(store, 'first public', None, conversation='public:2')
    say(store, 'second public', None, conversation='public:3')
    say(store, 'third public', None, conversation='public:4')
    say(store, 'here already', None, conversation='public:1')
    say(store, 'restricted', None, conversation='public:5', audience='conversation')
    say(store, 'a dm', None, conversation='private:9', audience='conversation')
    say(store, 'someone else', None, conversation='public:2', user='30')
    assert elsewhere(store, 'public:1') == [('public:3', 'second public'), ('public:4', 'third public')]
    assert elsewhere(store, 'private:9') == [('public:4', 'third public'), ('public:1', 'here already')]
    assert elsewhere(store, 'public:4') == [('public:3', 'second public'), ('public:1', 'here already')]
    turn = store.chat_context('123', 'public:9', '20')['elsewhere'][-1]
    assert set(turn) == {'conversation_id', 'input', 'at', 'provenance'}  # Their own words, never Fri's reply.


def test_a_reply_drawing_on_a_conversation_note_never_reaches_another_channel(store):
    # Said while #1 was restricted; #1 later opens to everyone and Fri's reply there uses the note.
    say(store, 'I like private jazz', 'music', conversation='public:1', audience='conversation')
    say(store, 'what music do i like', None, reply='you like private jazz')
    assert elsewhere(store, 'public:2') == [('public:1', 'what music do i like')]
    assert 'private jazz' not in str(store.chat_context('123', 'public:2', '20'))


def test_elsewhere_drops_turns_built_on_someone_who_changed_their_privacy(store):
    say(store, 'about ash', None, conversation='public:2', provenance=[ASH])
    say(store, 'my own thing', None, conversation='public:3')
    # Their words can repeat ash's, so what's built on them inherits ash's provenance.
    assert [turn['provenance'] for turn in store.chat_context('123', 'public:1', '20')['elsewhere']] == [[ASH], []]
    with store.connection() as db:  # A revision change without an eager delete.
        db.execute("INSERT INTO chat_revisions VALUES ('30',1)")
    assert elsewhere(store, 'public:1') == [('public:3', 'my own thing')]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM chat_turns WHERE input='about ash'").fetchone()[0] == 0


def test_memory_off_hides_and_forget_me_erases_every_scope(store):
    say(store)
    say(store, 'I like private jazz', 'music', conversation='private:9', audience='conversation')
    say(store, 'elsewhere chat', None, conversation='public:2')
    store.personal_control('20', 'disable')
    context = store.chat_context('123', 'public:3', '20')
    assert context['elsewhere'] == [] and context['personal_memory'] == {'enabled': False, 'notes': []}
    with store.connection() as db:
        for table in ('personal_notes', 'chat_turns'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE user_id=?', ('20',)).fetchone()[0] == 0
    store.personal_control('20', 'enable')
    say(store, revision=2)
    store.erase_user('20')
    assert notes(store, 'public:2') == []


def test_clear_chats_empties_elsewhere_but_keeps_the_profile(store):
    say(store, conversation='public:2')
    store.clear_chats('20')
    context = store.chat_context('123', 'public:1', '20')
    assert context['elsewhere'] == [] and [n['evidence'] for n in context['personal_memory']['notes']] == ['I like Minecraft']


def test_private_lists_everything_and_public_lists_what_it_can_see(store):
    say(store)
    say(store, 'I like restricted chess', conversation='public:5', audience='conversation')
    say(store, 'I like private jazz', 'music', conversation='private:9', audience='conversation')
    listed = entries(store, '123', 'private:9', '20')['entries']
    assert [(row['scope'], row['evidence']) for row in listed] == [
        ('server', 'I like Minecraft'), ('private:9', 'I like private jazz'), ('public:5', 'I like restricted chess')]
    assert listed[0]['origin'] == 'public:1' and listed[0]['expires_at'] - listed[0]['at'] == 90*86400
    assert [(row['scope'], row['evidence']) for row in entries(store, '123', 'public:2', '20')['entries']] == [
        ('server', 'I like Minecraft')]
    assert [row['scope'] for row in entries(store, '123', 'public:5', '20')['entries']] == ['server', 'public:5']


def private_source():
    return {**control_source(), 'channel_id': '9'}


def test_editing_a_profile_note_from_a_dm_clears_every_conversation(store):
    say(store)
    say(store, 'dm chat', None, conversation='private:9', audience='conversation')
    say(store, 'other chat', None, conversation='public:2')
    say(store, 'not this member', None, conversation='public:2', user='30')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    result = edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'replace', 'I prefer Terraria')
    assert [(row['scope'], row['evidence'], row['origin']) for row in result['entries']] == [
        ('server', 'I prefer Terraria', 'public:1')]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='30'").fetchone()[0] == 1
        # The removal fence is the profile's, so older text can't re-learn it there.
        assert db.execute('SELECT channel_id FROM personal_note_removals').fetchone()[0] == 'server'
    say(store, 'I like Minecraft again', revision=1)  # A fresh disclosure after the edit is kept.
    assert len(notes(store, 'public:3')) == 2


def test_editing_another_conversations_note_clears_only_that_conversation(store):
    say(store, 'I like restricted chess', conversation='public:5', audience='conversation')
    say(store, 'dm chat', None, conversation='private:9', audience='conversation')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    with store.connection() as db:
        assert [row[0] for row in db.execute("SELECT channel_id FROM chat_turns WHERE user_id='20'")] == ['private:9']


@pytest.mark.parametrize('scope,conversation,event_channel', [
    ('public:5', 'public:2', '2'),   # Another public conversation's note.
    ('private:9', 'public:2', '2'),  # A DM's note, from public.
])
def test_public_conversations_cannot_edit_notes_they_cannot_see(store, scope, conversation, event_channel):
    say(store, 'I like hidden chess', conversation=scope, audience='conversation')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    with pytest.raises(ValueError, match='no longer available'):
        edit(store, '123', conversation, '20', note['id'], 0, {**control_source(), 'channel_id': event_channel}, 'remove')


def test_removing_a_profile_note_takes_its_words_under_any_category_or_phrasing(store):
    before = time.time()-60
    say(store, 'I like Minecraft modpacks')
    say(store, 'I like Minecraft modpacks', 'hobbies', conversation='public:5', audience='conversation')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    assert entries(store, '123', 'private:9', '20')['entries'] == []
    with store.connection() as db:  # Older text can't bring it back shorter or under another category.
        for restated in (('hobbies', 'I like Minecraft modpacks'), ('nickname', 'I like Minecraft modpacks'),
                         ('hobbies', 'I like Minecraft'), ('games', 'I like modpacks')):
            assert not save(db, '123', 'public:2', '20', restated, before)


def test_downgrade_copies_profile_removals_to_every_channel_an_older_controller_learns_in(store, monkeypatch):
    before = time.time()-60
    say(store, conversation='public:1')
    say(store, 'chat', None, conversation='public:3', user='30')  # Editing clears member 20's own chats.
    with store.connection() as db:
        db.execute("INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES ('4','x','[]',0,'{}')")
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    monkeypatch.setenv('FRI_CHANNEL_IDS', '7, 8')
    monkeypatch.setenv('FRI_HISTORY_CHANNEL_IDS', '9')
    downgrade([str(store.path)])
    with store.connection() as db:
        fenced = {row[0] for row in db.execute('SELECT channel_id FROM personal_note_removals')}
        assert fenced == {'public:3', 'public:4', 'public:7', 'public:8', 'public:9'}
        for channel in sorted(fenced):
            for note in (('games', 'I like Minecraft'), ('games', 'I like chess'), ('hobbies', 'I like Mine'),
                         ('music', 'I like jazz')):
                assert not older_controller_saves(db, channel, note, before)
            assert older_controller_saves(db, channel, ('games', 'I like chess'), time.time()+1)


def test_downgrade_refuses_without_channels_while_profile_removals_exist(store, monkeypatch):
    say(store)
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    monkeypatch.delenv('FRI_CHANNEL_IDS', raising=False)
    monkeypatch.delenv('FRI_HISTORY_CHANNEL_IDS', raising=False)
    with pytest.raises(ValueError, match='configured channels'):
        store.downgrade_member_profiles()
    with pytest.raises(SystemExit, match='Nothing changed'):
        downgrade([str(store.path)])
    with pytest.raises(SystemExit, match='--channels'):
        downgrade([str(store.path), '--channel', '7'])
    with pytest.raises(SystemExit, match='Nothing changed. Channels are Discord channel ids'):
        downgrade([str(store.path), '--channels', 'public:7'])
    with store.connection() as db:
        assert {row[0] for row in db.execute('SELECT channel_id FROM personal_note_removals')} == {'server'}
    downgrade([str(store.path), '--channels', '7, 8'])
    with store.connection() as db:
        assert {row[0] for row in db.execute('SELECT channel_id FROM personal_note_removals')} == {'public:7', 'public:8'}


def test_downgrade_removes_profiles_and_keeps_exchanges_where_said(store):
    say(store)
    say(store, 'I like restricted chess', conversation='public:5', audience='conversation')
    say(store, 'chat', None, conversation='public:2')
    downgrade([str(store.path)])
    with store.connection() as db:
        assert [row[0] for row in db.execute('SELECT channel_id FROM personal_notes')] == ['public:5']
        assert {row[0] for row in db.execute('SELECT audience FROM chat_turns')} == {'conversation'}
    assert elsewhere(store, 'public:1') == []


def test_existing_database_gains_profile_columns_without_moving_anything(tmp_path):
    path = tmp_path/'old.sqlite3'
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE personal_notes DROP COLUMN origin')
        db.execute('ALTER TABLE chat_turns DROP COLUMN audience')
        db.execute("INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at) "
                   "VALUES ('123','public:1','20','games','I like chess',?)", (time.time(),))
    store = Store(path)
    assert notes(store, 'public:1') == [('here', 'I like chess')]
    assert notes(store, 'public:2') == []


def test_an_older_fri_gets_only_this_conversations_notes(store):
    categories = ('games', 'music', 'hobbies', 'conversation', 'nickname')
    for index in range(15):
        say(store, f'I like profile thing {index}', categories[index % 5])
        say(store, f'I like dm thing {index}', categories[index % 5], conversation='private:9', audience='conversation')
        say(store, f'I like staff thing {index}', categories[index % 5], conversation='public:5',
            audience='conversation')
    say(store, 'chat', None, conversation='public:2')
    assert notes(store, 'public:1', profiles=False) == []
    assert store.chat_context('123', 'public:1', '20', False)['elsewhere'] == []
    assert {scope for scope, _ in notes(store, 'public:5', profiles=False)} == {'here'}
    assert len(notes(store, 'public:5', profiles=False)) == 10  # The bound it always had.
    legacy = entries(store, '123', 'private:9', '20', False)['entries']
    assert {row['scope'] for row in legacy} == {'private:9'} and len(legacy) <= 10
    assert len(entries(store, '123', 'private:9', '20')['entries']) > 25  # Newer Fri pages its own view.
    profile_note = next(row for row in entries(store, '123', 'private:9', '20')['entries'] if row['scope'] == 'server')
    with pytest.raises(ValueError, match='no longer available'):
        edit(store, '123', 'private:9', '20', profile_note['id'], 0, private_source(), 'remove', profiles=False)


def test_context_and_lists_ask_for_profiles(controls_api):  # noqa: F811
    client, store = controls_api
    body = {key: control_body()[key] for key in ('guild_id', 'channel_id', 'user_id', 'conversation_id')}
    assert client.post('/chat/turn', headers=headers('fri'), json={
        **body, 'source': control_body()['source'], 'revision': 0, 'input': 'I like chess', 'reply': 'ok',
        'model': 'm', 'effort': 'high', 'provenance': [], 'audience': 'server',
        'notes': [{'category': 'games', 'evidence': 'I like chess'}]}).json()['memory_scope'] == 'server'
    other = {**body, 'conversation_id': 'public:'+str(int(body['conversation_id'].split(':')[1])+1)}
    for path, key in (('/chat/context', 'personal_memory'), ('/chat/memory/notes', 'entries')):
        older = client.post(path, headers=headers('fri'), json=other).json()[key]
        newer = client.post(path, headers=headers('fri'), json={**other, 'profiles': True}).json()[key]
        assert (older.get('notes') if isinstance(older, dict) else older) == []
        assert [row['evidence'] for row in (newer['notes'] if isinstance(newer, dict) else newer)] == ['I like chess']


def test_removing_a_profile_note_fences_learning_in_every_conversation(store):
    before = time.time()-60
    say(store, conversation='public:1')
    say(store, 'I like Minecraft', conversation='public:5', audience='conversation')  # A single-channel copy.
    say(store, 'I like private jazz', 'music', conversation='public:5', audience='conversation')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    assert note['scope'] == 'server'
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    assert notes(store, 'public:5') == [('here', 'I like private jazz')]  # The copy went with it.
    with store.connection() as db:
        # Historical learning in any channel, of the removed text or another older line in its category.
        for evidence in ('I like Minecraft', 'I like Terraria'):
            assert not save(db, '123', 'public:2', '20', ('games', evidence), before)
        assert not save(db, '123', 'public:2', '20', ('music', 'I like old jazz'), before)  # Restated anywhere.
        assert save(db, '123', 'public:2', '20', ('games', 'I like Minecraft'), time.time()+1)  # Said again.


def test_background_learning_drops_what_a_member_said_before_a_removal(store, tmp_path):
    before = time.time()-60
    say(store)
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    revision = entries(store, '123', 'private:9', '20')['revision']
    agent = learner(store, tmp_path)
    claim = agent.claim()
    member = {'user_id': '20', 'role_id': 'admin', 'revision': revision}
    records = [{**member, 'id': '100', 'text': 'I like Minecraft modpacks', 'at': before},
               {**member, 'id': '101', 'text': 'I like Terraria', 'at': time.time()},
               {**member, 'id': '102', 'user_id': '30', 'revision': 0, 'text': 'same', 'at': before}]
    result = learn(agent, claim, records, [{'source_id': '100', 'category': 'hobbies', 'evidence': 'I like Minecraft'},
                                           {'source_id': '101', 'category': 'games', 'evidence': 'I like Terraria'}],
                   summary='Member 20 likes Minecraft.')
    assert (result['eligible'], result['saved']) == (2, 1)
    assert notes(store, 'public:2') == [('here', 'I like Terraria')]  # Said after the removal.
    assert agent.context('2', '30')['summary'] == ''
    assert 'Minecraft' not in agent.reports()[0]['body']


def test_a_removal_lasts_as_long_as_archive_search_reaches_back(store):
    assert RANGES['exploration_days'][1]*86400 <= HORIZON
    old = time.time()-TTL-86400
    with store.connection() as db:
        for fence in fences():
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?)', ('123', 'server', '20', fence, old))
        db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?)',
                   ('123', 'public:1', '20', digest('games', 'I like chess'), old))
    notes(store, 'public:1')
    with store.connection() as db:
        assert {row[0] for row in db.execute('SELECT digest FROM personal_note_removals')} == set(fences())
        assert removed(db, '123', 'public:1', '20') == old


def test_the_member_profiles_capability_is_optional():
    advertised = contract()
    assert compatible(advertised)
    for optional in ('member-profiles', 'reply-checks'):
        assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != optional]})
    assert not compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'turn-provenance']})


def test_a_message_sent_before_a_removal_keeps_nothing_when_answered_after(store):
    say(store, 'I like jazz', 'music')
    early = source(age=3, channel='1')  # Waiting for a reply while the member opens My memory.
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    revision = entries(store, '123', 'private:9', '20')['revision']
    queued = store.save_chat('123', 'public:1', '20', revision, 'I like Minecraft', 'nice', 'model', 'high',
                             notes=[{'category': 'games', 'evidence': 'I like Minecraft'}], source=early,
                             provenance=[], audience='server')
    later = source(channel='1')
    burst = store.save_chat('123', 'public:1', '20', revision, 'anyway', 'ok', 'model', 'high', source=later,
                            burst=[{'source': early, 'text': 'I like Minecraft'}, {'source': later, 'text': 'anyway'}],
                            provenance=[], audience='server')
    assert queued == burst == {'ok': True, 'first_memory': False, 'kept': False}
    assert notes(store, 'public:2') == [] and elsewhere(store, 'public:2') == []
    with store.connection() as db:
        assert not db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0]
    assert say(store, revision=revision)['first_memory']  # Said after the removal, it counts.


@pytest.mark.parametrize('scope,cleared', [('server', {'public:1', 'public:5'}), ('public:5', {'public:5'})])
def test_editing_a_note_clears_research_that_quotes_the_member(store, scope, cleared):
    say(store, 'I like jazz', 'music', conversation='public:1' if scope == 'server' else scope,
        audience='server' if scope == 'server' else 'conversation')
    with store.connection() as db:
        for conversation in ('public:1', 'public:5'):
            db.execute('INSERT INTO web_research(guild_id,user_id,channel_id,created,receipt) VALUES (?,?,?,?,?)',
                       ('123', '20', conversation, time.time(), '{"query": "I like jazz. What else?"}'))
            db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)',
                       ('123', conversation, '20', '1', 0, time.time(), 'jazz like this'))
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    with store.connection() as db:
        for table in ('web_research', 'web_requests'):
            assert {row[0] for row in db.execute(f'SELECT channel_id FROM {table}')} == {'public:1', 'public:5'}-cleared


def test_removing_a_note_takes_overlapping_excerpts_with_it(store):
    say(store, 'I like Minecraft')
    say(store, 'I like Minecraft modpacks', 'hobbies', conversation='public:5', audience='conversation')
    say(store, 'I like minecraft', conversation='private:9', audience='conversation')
    say(store, 'I like jazz', 'music', conversation='public:5', audience='conversation')
    say(store, 'I like chess clubs', 'hobbies')
    say(store, 'I like chess openings', 'hobbies', conversation='public:6', audience='conversation')
    say(store, 'I like chess', conversation='public:6', audience='conversation')

    def listed():
        return {(row['scope'], row['evidence']) for row in entries(store, '123', 'private:9', '20')['entries']}

    def remove(scope, evidence):
        row = next(r for r in entries(store, '123', 'private:9', '20')['entries']
                   if (r['scope'], r['evidence']) == (scope, evidence))
        edit(store, '123', 'private:9', '20', row['id'], entries(store, '123', 'private:9', '20')['revision'],
             private_source(), 'remove')

    remove('server', 'I like Minecraft')  # The profile reached every conversation.
    assert listed() == {('public:5', 'I like jazz'), ('server', 'I like chess clubs'),
                        ('public:6', 'I like chess openings'), ('public:6', 'I like chess')}
    remove('public:6', 'I like chess')  # A conversation note, in its conversation only.
    assert listed() == {('public:5', 'I like jazz'), ('server', 'I like chess clubs')}


def test_a_mention_sent_before_a_removal_keeps_nothing_when_admitted_after(h):  # noqa: F811
    early = mention_source(age=3)
    with h.store.connection() as db:
        save(db, '123', 'server', '20', ('games', 'I like Minecraft'), time.time())
    note = entries(h.store, '123', 'private:9', '20')['entries'][0]
    edit(h.store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    _, scope, grant = invited(h, source=early, content='<@99> Minecraft release notes')
    receipt = {'query': 'Minecraft release notes', 'answer': 'A new update.',
               'sources': ['https://example.org/minecraft'], 'checked_at': '2026-10-25T00:00:00+00:00'}
    body = mention_turn(scope, revision=grant['revision'], input='Minecraft release notes', web_receipt=receipt)
    for _ in range(2):  # A retry answers the same.
        assert post(h, '/chat/turn', body).json() == {'ok': True, 'first_memory': False, 'kept': False}
    with h.store.connection() as db:
        assert db.execute('SELECT count(*) FROM chat_turns').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM web_research').fetchone()[0] == 0
    _, later, _ = invited(h)
    assert post(h, '/chat/context', later).json()['web_research'] == []


def test_fri_learns_when_a_member_last_edited_their_memory(store, tmp_path):
    agent = learner(store, tmp_path)
    assert agent.context('2', '20')['removed_at'] == 0
    say(store, 'I like jazz', 'music', conversation='private:9', audience='conversation')
    note = entries(store, '123', 'private:9', '20')['entries'][0]
    before = time.time()
    edit(store, '123', 'private:9', '20', note['id'], 0, private_source(), 'remove')
    assert before <= agent.context('2', '20')['removed_at'] <= time.time()


def test_a_refused_downgrade_leaves_an_older_database_untouched(tmp_path):
    path = tmp_path/'old.sqlite3'
    Store(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE personal_notes DROP COLUMN origin')
        db.execute('ALTER TABLE chat_turns DROP COLUMN audience')
        schema = db.execute('SELECT group_concat(sql) FROM sqlite_master').fetchone()[0]
    with pytest.raises(SystemExit, match='Nothing changed. Channels are Discord channel ids'):
        downgrade([str(path), '--channels', 'public:7'])
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT group_concat(sql) FROM sqlite_master').fetchone()[0] == schema
    with pytest.raises(SystemExit, match='Nothing changed. .* is not a readable controller database'):
        downgrade([str(tmp_path/'missing.sqlite3'), '--channels', '7'])
    assert not (tmp_path/'missing.sqlite3').exists()


def test_a_retried_mention_save_answers_as_the_first_did(h):  # noqa: F811
    _, scope, grant = invited(h)
    body = mention_turn(scope, revision=grant['revision'])
    assert post(h, '/chat/turn', body).json() == post(h, '/chat/turn', body).json() == {'ok': True, 'first_memory': False}
    h.store.personal_control('20', 'disable')
    _, scope, grant = invited(h)
    body = mention_turn(scope, revision=grant['revision'])
    for _ in range(2):
        assert post(h, '/chat/turn', body).json() == {'ok': True, 'first_memory': False, 'kept': False}
