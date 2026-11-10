import asyncio
import json

import pytest

from gene.fri_chat import Chat
from tests.test_fri_chat import CONTEXT, response_event
from tests.test_fri_web import transport


@pytest.mark.parametrize('action,expected', [('play', 'completed'), ('eval', 'invalid_output'), ('audioset', 'invalid_output')])
def test_model_cannot_choose_arbitrary_red_commands(action, expected):
    result = {'reply': '', 'request_draft': None, 'notes': [],
              'action': {'kind': 'gene_command', 'action': action, 'argument': 'Sad Machine'}}
    chat = Chat(type('Auth', (), {'access_token': lambda _: 'test'})(), transport([response_event(content=json.dumps(result))]))
    output = asyncio.run(chat.respond('fri play Sad Machine', {**CONTEXT, 'actions': {'gene_command': {'commands': {'play': ''}}}}))
    assert output.status == expected
