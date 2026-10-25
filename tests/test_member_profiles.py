"""One profile per member across the server's readable channels; private conversations stay put."""

import sqlite3
import time

import pytest

from gene.fri_memory_store import edit, entries
from gene.profile_downgrade import main as downgrade
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_fri_memory_items import source as control_source
from tests.test_gene_controls import control_body, controls_api  # noqa: F401

ASH = {'user_id': '30', 'revision': 0}


def say(store, text='I like Minecraft', category='games', conversation='public:1', audience='server', user='20',
        reply='noted', provenance=(), revision=0, **extra):
    return store.save_chat('123', conversation, user, revision, text, reply, 'model', 'high',
                           notes=[{'category': category, 'evidence': text}] if category else [],
                           source=source(user=user, channel=conversation.split(':')[1]),
                           provenance=list(provenance), audience=audience, **extra)


def notes(store, conversation, user='20'):
    return [(note['scope'], note['evidence']) for note in
            store.chat_context('123', conversation, user)['personal_memory']['notes']]


def elsewhere(store, conversation, user='20'):
    return [(turn['conversation_id'], turn['input']) for turn in store.chat_context('123', conversation, user)['elsewhere']]


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
    assert set(turn) == {'conversation_id', 'input', 'reply', 'at', 'provenance'}


def test_elsewhere_drops_turns_built_on_someone_who_changed_their_privacy(store):
    say(store, 'about ash', None, conversation='public:2', provenance=[ASH])
    say(store, 'my own thing', None, conversation='public:3')
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
