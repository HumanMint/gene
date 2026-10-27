"""A member putting right the song Gene just played for them ("not that one, X") gets X queued, once."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import discord
import pytest

from gene.control_store import Controls
from gene.gene_controls import correction
from tests.test_gene_controls import control_body
from tests.test_runtime_controls import runtime

FIX = 'not the one, i meant join us for a bite'


@pytest.mark.parametrize('text,argument,expected', [
    (FIX, 'join us for a bite', True),
    ('fri not that one lol, Join Us For A Bite', 'join us for a bite', True),
    ('wrong song, "join us for a bite"', 'join us for a bite', True),  # Quoted song names are still theirs.
    ('i meant join us for a bite', 'join us for a bite', True),
    ('join us for a bite', 'join us for a bite', False),  # No correction: a plain title isn't a request.
    (FIX, 'someone else song', False),  # Not in their words.
    ('for example: not that one, i meant join us for a bite', 'join us for a bite', False),
    ('"not that one, i meant join us for a bite" is what he said', 'join us for a bite', False),
    ("not that one, don't play join us for a bite", 'join us for a bite', False),
    (FIX, '', False),
])
def test_correction_needs_the_words_and_the_song_in_their_own_message(text, argument, expected):
    assert correction(text, argument) is expected


def played(store, **changes):
    """The member's earlier play, finished by Gene a minute ago."""
    controls = Controls(store)
    first = control_body(**{'source': {**control_body()['source'], 'id': str(discord.utils.time_snowflake(
        dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))}, **changes})
    controls.enqueue(first)
    claim = controls.poll(['play'])
    controls.finish(claim['id'], claim['lease'], {'output': 'Track Enqueued'})
    with store.connection() as db:
        db.execute('UPDATE gene_controls SET created=created-60')
    return claim['id']


def fix(**changes):
    return control_body(**{'request_text': FIX, 'argument': 'join us for a bite', **changes})


def test_a_correction_after_their_own_play_is_queued_once(store):
    prior = played(store)
    controls = Controls(store)
    controls.enqueue(fix())
    claim = controls.poll(['play'])
    assert claim['argument'] == 'join us for a bite' and claim['correction_of'] == prior
    with store.connection() as db:
        db.execute('UPDATE gene_controls SET created=created-10')
    # One correction per play, even from a fresh message. (Once Gene finishes this one, it's the latest play,
    # and "not that one either" can put it right in turn.)
    with pytest.raises(ValueError):
        controls.enqueue(fix(source={**control_body()['source'], 'id': str(int(claim['source']['id'])+(1 << 22))}))


@pytest.mark.parametrize('change', ['member', 'conversation', 'old', 'unfinished', 'not play', 'words', 'none'])
def test_a_correction_needs_their_own_recent_finished_play_here(store, change):
    if change == 'member':
        played(store, user_id='21', source={**control_body()['source'], 'user_id': '21',
               'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))})
    elif change == 'conversation':
        played(store, conversation_id='public:2', channel_id='2', target_channel_id='2',
               source={**control_body()['source'], 'channel_id': '2',
                       'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))})
    elif change == 'not play':
        played(store, action='skip', argument='', request_text='fri skip the song')
    elif change != 'none':
        played(store)
    with store.connection() as db:
        if change == 'old':
            db.execute('UPDATE gene_controls SET created=created-600')
        elif change == 'unfinished':
            db.execute("UPDATE gene_controls SET state='claimed'")
    body = fix(argument='someone else song') if change == 'words' else fix()
    with pytest.raises(ValueError):
        Controls(store).enqueue(body)


def test_gene_runs_a_correction_the_controller_found_and_nothing_else_without_a_request():
    async def scenario():
        bot, runner, cog, claim, member, channel, calls, message = await runtime()
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        message.content = FIX
        try:
            plain = {**claim, 'request_text': FIX, 'argument': 'join us for a bite'}
            assert 'blocked' in await runner.execute(cog, plain)
            assert not calls
            result = await runner.execute(cog, {**plain, 'correction_of': 'earlier'})
            assert 'Track Enqueued' in result['output'] and len(calls) == 1
        finally:
            await bot.close()
    asyncio.run(scenario())
