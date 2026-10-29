"""Exercise the trusted API boundary with a real synthetic controller database."""

import itertools
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import snowflake_floor
from gene.fri_mentions import BREADCRUMB_INPUT, BREADCRUMB_REPLY
from tests.test_api import headers

IDS = itertools.count(1)


def source(user='20', channel='789', guild='123', age=0):
    return {'kind': 'discord_message', 'id': str(int(snowflake_floor(time.time()-age))+next(IDS)),
            'user_id': user, 'display_name': 'member '+user, 'channel_id': channel, 'guild_id': guild}


@pytest.fixture
def h(tmp_path):
    settings = Settings(tmp_path/'mention.sqlite3', '123', '10',
                        'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                        fri_token='fri-'+'x'*32, workshop_channel_id='456',
                        test_guild_id='999', test_channel_id='111')
    app = create_app(settings)
    app.state.agent.update({'enabled': True}, 0, '10')
    return SimpleNamespace(client=TestClient(app), store=app.state.store,
                           agent=app.state.agent, settings=settings)


def admission(**changes):
    event = changes.pop('source', source())
    return {'guild_id': event['guild_id'], 'user_id': event['user_id'], 'channel_id': event['channel_id'],
            'conversation_id': 'public:'+event['channel_id'], 'source': event,
            'content': '<@99> what is in this picture?', 'bot_id': '99', **changes}


def post(h, path, body, role='fri'):
    return h.client.post(path, headers=headers(role), json=body)


def invited(h, **changes):
    body = admission(**changes)
    response = post(h, '/chat/mentions/admit', body)
    assert response.status_code == 200, response.text
    grant = response.json()
    assert grant['allowed']
    scope = {key: body[key] for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id', 'source')}
    return body, {**scope, 'mention_grant': grant['mention_grant']}, grant


def turn(scope, **changes):
    return {**scope, 'revision': 0, 'input': 'what is in this picture?', 'reply': 'A small frog.',
            'model': 'test-model', 'effort': 'high', **changes}


@pytest.mark.parametrize('content', ['<@99> hi', 'hi <@!99>', 'someone asked; <@99> what do you think?'])
def test_only_real_unquoted_mention_can_admit_and_does_not_expand_allowlist(h, content):
    body, scope, grant = invited(h, content=content)
    assert len(grant['mention_grant']) == 64 and 0 < grant['expires_at']-time.time() <= 300
    assert not h.settings.chat_channel_allowed('123', '789')
    assert post(h, '/chat/context', scope).status_code == 200
    assert post(h, '/chat/context', {key: value for key, value in scope.items()
                                   if key != 'mention_grant'}).status_code == 403
    # Refetches of the same gateway event recover the reservation without charging it twice.
    assert post(h, '/chat/mentions/admit', body).json() == grant
    with h.store.connection() as db:
        assert db.execute('SELECT turns FROM agent_sessions WHERE user_id="20" AND space="public"').fetchone()[0] == 1


@pytest.mark.parametrize('content', ['fri hello', '<@999> hi', '`<@99>`', '```\n<@99>\n```',
    '~~~\n<@99>\n~~~', '```<@99>', '> <@99> hello', '>>> quoted\n<@99>',
    'https://example.org/<@99>', '"<@99>"', '“<@99>”', "'<@99>'", r'\<@99>'])
def test_text_code_quotes_urls_and_escaped_mentions_are_not_invitations(h, content):
    assert post(h, '/chat/mentions/admit', admission(content=content)).status_code == 403
    with h.store.connection() as db:
        assert db.execute('SELECT count(*) FROM mention_grants').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM agent_sessions').fetchone()[0] == 0


@pytest.mark.parametrize('role', ['runtime', 'worker', 'deploy'])
def test_other_service_credentials_cannot_create_or_use_mention_grants(h, role):
    _, scope, _ = invited(h)
    for path, body in [('/chat/mentions/admit', admission()), ('/chat/context', scope),
                       ('/chat/mentions/profiles', {**scope, 'evidence': []}), ('/chat/turn', turn(scope)),
                       ('/chat/research/start', {**scope, 'revision': 0, 'input': 'weather in Seoul',
                                                'query': 'weather in Seoul'})]:
        assert post(h, path, body, role).status_code == 401


@pytest.mark.parametrize('change', [lambda: {'source': source(guild='999', channel='111')},
    lambda: {'source': source(guild='1000')}, lambda: {'source': source(guild=None)},
    lambda: {'source': source(age=301)}, lambda: {'source': source(age=-10)},
    lambda: {'channel_id': '456'}, lambda: {'user_id': '21'},
    lambda: {'conversation_id': 'public:456'}])
def test_admission_rejects_other_servers_dm_old_events_and_false_identity(h, change):
    response = post(h, '/chat/mentions/admit', admission(**change()))
    assert response.status_code in (400, 403, 422)


def test_grant_rejects_new_event_author_channel_parent_guild_and_dm(h):
    _, scope, _ = invited(h)
    for change in ({'source': source()}, {'user_id': '21'}, {'channel_id': '456'},
                   {'conversation_id': 'public:456'}, {'guild_id': '999'},
                   {'conversation_id': 'private:789'}, {'source': None}):
        assert post(h, '/chat/context', {**scope, **change}).status_code in (400, 403)
    changed_source = {**scope['source'], 'kind': 'discord_interaction'}
    assert post(h, '/chat/context', {**scope, 'source': changed_source}).status_code == 403
    assert post(h, '/chat/turn', turn(scope, input='different question')).status_code == 403
    # A thread invitation uses that actual channel; its parent is never granted.
    _, thread, _ = invited(h, source=source(channel='790'))
    assert post(h, '/chat/context', thread).status_code == 200
    assert post(h, '/chat/context', {**thread, 'channel_id': '789'}).status_code == 403


def test_expired_or_invalid_grant_never_falls_back_to_configured_channel(h):
    body, scope, _ = invited(h, source=source(channel='456'))
    assert post(h, '/chat/context', {**scope, 'mention_grant': '0'*64}).status_code == 403
    with h.store.connection() as db:
        db.execute('UPDATE mention_grants SET expires=?', (time.time()-1,))
    for path, payload in [('/chat/context', scope), ('/chat/turn', turn(scope)),
                          ('/chat/mentions/profiles', {**scope, 'evidence': []})]:
        assert post(h, path, payload).status_code == 403
    assert post(h, '/chat/mentions/admit', body).status_code == 403
    ordinary = {key: value for key, value in scope.items() if key not in ('mention_grant', 'source')}
    assert post(h, '/chat/context', ordinary).status_code == 200
    for path, payload in [('/chat/pending', scope), ('/chat/memory/notes', scope),
                          ('/chat/memory/control', {**scope, 'action': 'disable'})]:
        assert post(h, path, payload).status_code == 403


def test_one_neutral_turn_is_idempotent_and_readable_for_final_privacy_fence(h):
    body, scope, _ = invited(h)
    payload = turn(scope)
    assert post(h, '/chat/turn', payload).status_code == 200
    assert post(h, '/chat/turn', payload).status_code == 200
    assert post(h, '/chat/turn', {**payload, 'reply': 'changed reply'}).status_code == 409
    assert post(h, '/chat/mentions/admit', body).status_code == 403
    assert post(h, '/chat/context', scope).status_code == 200
    assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': []}).status_code == 200
    assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': [source(user='21')]}).status_code == 403
    with h.store.connection() as db:
        rows = db.execute('SELECT * FROM chat_turns').fetchall()
        assert len(rows) == 1
        assert rows[0]['input'] == BREADCRUMB_INPUT and rows[0]['reply'] == BREADCRUMB_REPLY
        assert rows[0]['burst'] == '[]' and json.loads(rows[0]['source']) == scope['source']
        assert db.execute('SELECT count(*) FROM personal_notes').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM chat_pending').fetchone()[0] == 0


def test_mention_context_cannot_reveal_existing_approved_channel_memories(h):
    _, scope, _ = invited(h, source=source(channel='456'))
    h.store.remember('123', '10', 'An owner-approved fact', '')
    h.store.save_chat('123', 'public:456', '20', 0, 'Old input with a quote', 'Old derived reply', 'm', 'high')
    context = post(h, '/chat/context', scope).json()
    assert context['turns'] == context['facts'] == context['personal_memory']['notes'] == []


@pytest.mark.parametrize('kind,extra', [('quiet', {}), ('react', {'reaction': '👍'})])
def test_mention_turn_cannot_acknowledge_without_a_text_answer_or_spend_grant(h, kind, extra):
    _, scope, _ = invited(h)
    assert post(h, '/chat/turn', turn(scope, response_kind=kind, reply='', **extra)).status_code == 403
    assert post(h, '/chat/turn', turn(scope)).status_code == 200


@pytest.mark.parametrize('action', ['clear', 'disable', 'erase'])
def test_privacy_invalidates_grant_and_same_event_cannot_renew_it(h, action):
    body, scope, _ = invited(h)
    if action == 'clear':
        h.store.clear_chats('20')
    elif action == 'disable':
        h.store.personal_control('20', 'disable')
    else:
        assert post(h, '/privacy/erase', {'guild_id': '123', 'user_id': '20'}).status_code == 200
    assert post(h, '/chat/context', scope).status_code == 403
    assert post(h, '/chat/turn', turn(scope)).status_code == 403
    assert post(h, '/chat/mentions/admit', body).status_code == 403
    _, fresh, grant = invited(h)
    assert grant['revision'] == 1
    assert post(h, '/chat/context', fresh).status_code == 200


def test_memory_opt_out_still_allows_ephemeral_reply_without_retention(h):
    h.store.personal_control('20', 'disable')
    _, scope, grant = invited(h)
    assert post(h, '/chat/context', scope).json()['personal_memory'] == {'enabled': False, 'notes': []}
    assert post(h, '/chat/turn', turn(scope, revision=grant['revision'])).status_code == 200
    assert post(h, '/chat/context', scope).status_code == 200
    with h.store.connection() as db:
        assert db.execute('SELECT count(*) FROM chat_turns').fetchone()[0] == 0


def test_bounded_profiles_only_return_same_channel_author_privacy_and_fence_changes(h):
    h.store.personal_control('22', 'disable')
    _, scope, _ = invited(h)
    evidence = [source(user='21', age=899), source(user='22', age=6*86400)]
    profiles = post(h, '/chat/mentions/profiles', {**scope, 'evidence': evidence})
    assert profiles.status_code == 200
    assert profiles.json() == {'profiles': {'20': {'enabled': True, 'revision': 0},
                                         '21': {'enabled': True, 'revision': 0},
                                         '22': {'enabled': False, 'revision': 1}}}
    for bad in ([source(user='23', channel='456')], [source(user='23', guild='999')],
                [source(user='23', age=7*86400+1)]):
        assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': bad}).status_code in (400, 403)
    assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': evidence*13}).status_code == 422
    assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': [], 'user_ids': ['23']}).status_code == 422
    h.store.erase_user('21')
    assert post(h, '/chat/context', scope).status_code == 403
    assert post(h, '/chat/turn', turn(scope)).status_code == 403


def test_erasure_after_save_blocks_final_delivery_without_retaining_context(h):
    _, scope, _ = invited(h)
    assert post(h, '/chat/mentions/profiles', {**scope, 'evidence': [source(user='21')]}).status_code == 200
    assert post(h, '/chat/turn', turn(scope, reply='A reply derived from member 21 context')).status_code == 200
    h.store.erase_user('21')
    assert post(h, '/chat/context', scope).status_code == 403
    with h.store.connection() as db:
        saved = db.execute('SELECT input,reply,burst FROM chat_turns').fetchone()
        assert list(saved) == [BREADCRUMB_INPUT, BREADCRUMB_REPLY, '[]']


def test_grants_share_public_member_allowance_across_channels_and_disabled_state(h):
    limit = h.agent.state()['config']['public_turns']
    for _ in range(limit-1):
        assert h.agent.admit('20', 'public', '123')['allowed']
    invited(h)
    denied = post(h, '/chat/mentions/admit', admission(source=source(channel='790'))).json()
    assert denied['allowed'] is False and 'mention_grant' not in denied
    h.agent.update({'enabled': False}, h.agent.state()['version'], '10')
    assert post(h, '/chat/mentions/admit', admission(source=source(user='30'))).status_code == 403


def test_disabling_and_reenabling_fri_does_not_restore_old_event_authority(h):
    body, scope, _ = invited(h)
    h.agent.update({'enabled': False}, h.agent.state()['version'], '10')
    assert post(h, '/chat/context', scope).status_code == 403
    h.agent.update({'enabled': True}, h.agent.state()['version'], '10')
    assert post(h, '/chat/context', scope).status_code == 403
    assert post(h, '/chat/mentions/admit', body).status_code == 403


def test_grants_do_not_admit_pending_notes_controls_workspace_social_or_requests(h):
    _, scope, _ = invited(h)
    for path, payload in [('/chat/pending', scope), ('/chat/pending/cancel', {**scope, 'input': 'cancel'}),
                          ('/chat/memory/control', {**scope, 'action': 'disable'}), ('/chat/memory/notes', scope),
                          ('/chat/operations/admit', {**scope, 'revision': 0, 'input': 'play music'}),
                          ('/controls/health', scope), ('/workspace/list', scope),
                          ('/social/reminders/list', scope),
                          ('/requests', {**scope, 'source_id': '123', 'source_url': '', 'body': 'Add a feature'})]:
        assert post(h, path, payload).status_code in (403, 422), path
    for extra in ({'notes': [{'category': 'fact', 'evidence': 'member likes frogs'}]},
                  {'allow_closing': True}, {'burst': [{'source': scope['source'], 'text': 'hi'}]}):
        assert post(h, '/chat/turn', turn(scope, **extra)).status_code == 403


def test_public_research_uses_exact_event_and_cannot_continue_after_turn(h):
    _, scope, _ = invited(h, content='<@99> weather in Seoul')
    research = {**scope, 'revision': 0, 'input': 'weather in Seoul', 'query': 'weather in Seoul'}
    assert post(h, '/chat/research/start', research).status_code == 200
    assert post(h, '/chat/research/start', {**research, 'source': source()}).status_code == 403
    assert post(h, '/chat/research/start', {**research, 'query': 'private saved notes'}).status_code == 400
    receipt = {'query': 'weather in Seoul', 'answer': 'Clear today.',
               'sources': ['https://example.org/weather'], 'checked_at': '2026-10-23T00:00:00+00:00'}
    assert post(h, '/chat/turn', turn(scope, input='weather in Seoul', web_receipt=receipt)).status_code == 200
    assert post(h, '/chat/context', scope).json()['web_research'] == [receipt]
    assert post(h, '/chat/research/start', research).status_code == 409


@pytest.mark.parametrize('prior_permit', [False, True])
def test_mention_research_cannot_reuse_pending_topic_from_former_configured_scope(h, prior_permit):
    original = source(channel='456', age=2)
    normal = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'public:456',
              'source': original}
    assert post(h, '/chat/turn', turn(normal, input='weather in Seoul', reply='For which day?',
        clarification={'goal': 'web', 'topic': 'weather in Seoul', 'question': 'For which day?'})).status_code == 200
    current = source(channel='456')
    if prior_permit:
        # A normal-scope pending clarification may already have authorized this
        # event before the channel was removed. That permit cannot survive into
        # the narrower mention-only authority.
        assert post(h, '/chat/research/start', {**normal, 'source': current, 'revision': 0,
            'input': 'hello', 'query': 'weather in Seoul'}).status_code == 200
    restricted = replace(h.settings, workshop_channel_id='')
    h.client = TestClient(create_app(restricted))
    _, scope, _ = invited(h, source=current, content='<@99> hello')
    assert post(h, '/chat/context', scope).json()['pending'] is None
    research = post(h, '/chat/research/start', {**scope, 'revision': 0, 'input': 'hello',
                                              'query': 'weather in Seoul'})
    receipt = {'query': 'weather in Seoul', 'answer': 'Clear today.',
               'sources': ['https://example.org/weather'], 'checked_at': '2026-10-23T00:00:00+00:00'}
    saved = post(h, '/chat/turn', turn(scope, input='hello', web_receipt=receipt))
    assert (research.status_code, saved.status_code) == (400, 400)
    with h.store.connection() as db:
        assert db.execute('SELECT count(*) FROM web_research').fetchone()[0] == 0


def test_existing_lab_scope_stays_exact_and_no_mention_invitation_expands_it(h):
    settings = replace(h.settings, agent_channels='456')
    client = TestClient(create_app(settings))
    for channel in ('111', '112'):
        body = admission(source=source(channel=channel, guild='999'))
        assert client.post('/chat/mentions/admit', headers=headers('fri'), json=body).status_code == 403
    scope = {'guild_id': '999', 'user_id': '20', 'channel_id': '111', 'conversation_id': 'public:111'}
    assert client.post('/chat/context', headers=headers('fri'), json=scope).status_code == 200
    assert client.post('/chat/context', headers=headers('fri'), json={**scope, 'channel_id': '112'}).status_code == 403


def test_grant_cannot_outlive_controller_main_guild_configuration(h):
    _, scope, _ = invited(h, content='<@99> weather in Seoul')
    h.client = TestClient(create_app(replace(h.settings, guild_id='1000')))
    for path, body in [('/chat/context', scope), ('/chat/mentions/profiles', {**scope, 'evidence': []}),
                       ('/chat/turn', turn(scope, input='weather in Seoul')),
                       ('/chat/research/start', {**scope, 'revision': 0, 'input': 'weather in Seoul',
                                                'query': 'weather in Seoul'})]:
        assert post(h, path, body).status_code == 403, path


def test_a_mention_saves_fingerprint_is_the_one_an_older_controller_kept(h):
    """gene #97 review: the summary generation, which a mention turn never carries, isn't in its fingerprint, so a
    retry across an upgrade or a rollback answers as the first save did."""
    from gene.api import ChatTurn
    from gene.fri_mentions import _hash
    _, scope, _ = invited(h)
    payload = turn(scope)
    assert post(h, '/chat/turn', payload).status_code == 200
    older = _hash(ChatTurn.model_validate(payload).model_dump_json(exclude={'summary_generation', 'summary_dependencies'}))
    with h.store.connection() as db:
        assert db.execute('SELECT turn_hash FROM mention_grants').fetchone()[0] == older
    assert post(h, '/chat/turn', payload).status_code == 200
    for generation in (None, 0, 7):  # Whatever it says, a field a mention never uses doesn't change it.
        assert post(h, '/chat/turn', {**payload, 'summary_generation': generation}).status_code == 200
    for dependencies in ([], [{'user_id': '30', 'generation': 7}]):  # Nor do these (fri #193).
        assert post(h, '/chat/turn', {**payload, 'summary_dependencies': dependencies}).status_code == 200
