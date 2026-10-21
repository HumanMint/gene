import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands

from gene.fri_chat import ChatResult
from gene.fri_errors import ControllerRejected
from gene.fri_outcomes import fallback, receipt
from gene.fri_runner import run
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_actions import harness
from tests.test_fri_runtime import event
from tests.test_gene_controls import control_body, controls_api  # noqa: F401


@pytest.mark.parametrize('value,outcome', [
    ({'output': 'Track enqueued'}, 'completed'),
    ({'blocked': 'DJ permission required'}, 'rejected'),
    ({'error': 'Search unavailable'}, 'failed'),
    ({'uncertain': 'Gene restarted'}, 'uncertain'),
    ({'done': True}, 'uncertain'),
])
def test_outcomes_distinguish_known_refusal_failure_and_uncertainty(value, outcome):
    actual = receipt('gene_command', value)
    assert actual['outcome'] == outcome
    text = fallback([actual])
    assert 'playing' not in text.casefold()
    if outcome == 'completed':
        assert 'Track enqueued' in text
    elif outcome == 'rejected':
        assert 'DJ permission required' in text and 'uncertain' not in text
    elif outcome == 'uncertain':
        assert 'not retried' in text


@pytest.mark.parametrize('failure', ['rejected', 'transport', 'model', 'deadline', 'active_deadline', 'repeat'])
def test_confirmed_tool_result_survives_followup_failure_without_reexecution(failure):
    async def scenario():
        action = {'kind': 'work_save', 'body': 'milk'}
        execute = AsyncMock(return_value={'id': 'a'*32, 'version': 2, 'state': 'open'})
        if failure == 'rejected':
            execute.side_effect = ControllerRejected('Saved work changed. Read its latest version.')
        elif failure == 'transport':
            execute.side_effect = commands.UserFeedbackCheckFailure('connection reset')
        elif failure == 'active_deadline':
            execute.side_effect = TimeoutError
        chat = AsyncMock(side_effect=TimeoutError if failure == 'deadline' else
                         commands.UserFeedbackCheckFailure('model unavailable'))
        if failure == 'repeat':
            chat = AsyncMock(return_value=ChatResult(status='completed', action=action))
        result, receipts, research = await run(SimpleNamespace(chat_once=chat),
            SimpleNamespace(execute=execute), 'save milk', {'actions': {'work_save': {}}},
            ChatResult(action=action, status='completed'))
        execute.assert_awaited_once()
        assert result.status == 'completed' and not result.action and not research
        expected = {'rejected': 'rejected', 'transport': 'uncertain', 'active_deadline': 'uncertain'}.get(failure, 'completed')
        assert receipts[-1]['outcome'] == expected
        if expected == 'completed':
            assert 'Saved your work at version 2' in result.reply
        elif expected == 'rejected':
            assert 'Saved work changed' in result.reply and 'uncertain' not in result.reply
        else:
            assert 'not retried' in result.reply
    asyncio.run(scenario())


def test_saved_work_remains_persisted_after_final_model_failure_and_restart(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, _, _, _ = harness(monkeypatch, tmp_path)
        calls = []
        async def api(method, path, body=None):
            calls.append(path)
            response = client.request(method, path, headers=headers('fri'), json=body)
            if response.status_code >= 400:
                raise ControllerRejected(response.json()['detail'])
            return response.json()
        fri.api = api
        actual = event(user=20, channel=1, content='save milk and bread')
        actual.id = int(control_body()['source']['id'])
        actual.webhook_id = None
        actual.channel.fetch_message = AsyncMock(return_value=actual)
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'work_save', 'work_id': '', 'version': 0,
                'title': 'groceries', 'body': 'milk and bread', 'state': 'open'}),
            ChatResult(status='failed', error='model disconnected')])
        try:
            result = await fri.talk(123, 1, 20, actual.content, 'public', event=actual)
            assert result.reply == 'Saved your work at version 1 (open).'
            restarted = Store(controls.store.path)
            from gene.fri_workspace_store import Workspace
            scope = {'guild_id': '123', 'user_id': '20', 'conversation_id': 'public:1'}
            work = Workspace(restarted).read(scope)['items']
            assert len(work) == 1
            assert Workspace(restarted).read(scope, work[0]['id'])['body'] == 'milk and bread'
            assert calls.count('/workspace/save') == 1
            context = restarted.chat_context('123', 'public:1', '20')
            assert 'work_save' in context['turns'][-1]['reply']
            assert 'milk and bread' not in context['turns'][-1]['reply']
        finally:
            await fri.close()
    asyncio.run(scenario())


def test_later_failure_keeps_prior_saved_work_confirmation():
    evidence = [receipt('work_save', {'version': 3, 'state': 'open'}),
                receipt('prepare_post', {'blocked': 'Channel access changed'})]
    result = fallback(evidence)
    assert 'Saved your work at version 3' in result and 'Channel access changed' in result


@pytest.mark.parametrize('state,expected', [('cancelled', 'completed'), ('sending', 'rejected'), ('sent', 'rejected')])
def test_reminder_cancellation_fallback_never_claims_an_inflight_send_was_cancelled(state, expected):
    evidence = receipt('reminder_cancel', {'state': state})
    assert evidence['outcome'] == expected
    assert ('Reminder cancelled.' in fallback([evidence])) is (state == 'cancelled')
