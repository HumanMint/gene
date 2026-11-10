"""Exercise reply privacy together with current images, reminders and social erasure."""

import asyncio
import json
from types import SimpleNamespace

import httpx

from gene.fri_chat import Chat
from gene.fri_social_store import Social
from tests.test_fri_chat import CONTEXT, response_event
from tests.test_fri_social import DM, PUBLIC, api, confirmed, future, source  # noqa: F401


def test_text_provenance_and_images_coexist_in_the_same_billed_model_request():
    async def scenario():
        target = [{'source': source(public=True, user='30').model_dump(), 'text': 'a referenced suggestion'}]
        burst = [{'source': source(public=True).model_dump(), 'text': 'fri what does this image show?'}]
        seen = []
        def respond(request):
            payload = json.loads(request.content)
            seen.append(payload)
            content = payload['input'][-1]['content']
            supplied = json.loads(content[0]['text'])
            assert supplied['reply_target'] == target and supplied['message_burst'] == burst
            assert content[1] == {'type': 'input_image', 'image_url': 'data:image/png;base64,aGVsbG8=', 'detail': 'auto'}
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps({
                'reply': 'it shows an error', 'request_draft': None})))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(respond))
        result = await chat.respond('fri what does this image show?', {**CONTEXT,
            'reply_target': target, 'message_burst': burst, 'images': ['data:image/png;base64,aGVsbG8=']})
        assert result.status == 'completed' and result.counters['input_tokens'] == 100 and len(seen) == 1
    asyncio.run(scenario())


def test_forget_erases_personal_notes_and_social_rows_without_removing_other_members_work(store):
    social = Social(store)
    store.save_chat('123', 'private:999', '10', 0, 'I like Minecraft', 'noted', 'model', 'high',
                    notes=[{'category': 'games', 'evidence': 'I like Minecraft'}])
    social.reminder_prepare(DM, source(1), 0, 'update mods', future(), 'UTC')
    social.plan_prepare(PUBLIC, source(2, public=True), 0, 'Helldivers', future()+'\n'+future(2),
                        'UTC', 'help us choose times for Helldivers')
    other = {**DM, 'user_id': '11', 'conversation_id': 'private:1000'}
    social.reminder_prepare(other, source(3, user='11', channel='1000'), 0, 'keep this reminder', future(), 'UTC')
    store.personal_control('10', 'disable')
    assert store.chat_context('123', 'private:999', '10')['personal_memory'] == {'enabled': False, 'notes': []}
    with store.connection() as db:
        for table in ('fri_reminders', 'fri_plans', 'fri_plan_votes', 'fri_social_events', 'personal_notes', 'personal_note_removals'):
            assert not db.execute('SELECT 1 FROM '+table+' WHERE user_id=?', ('10',)).fetchone()
    assert social.reminder_list(other)['items'][0]['body'] == 'keep this reminder'
