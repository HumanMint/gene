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
    # Review of #98/#157: the song is the start of the replacement, never words from a later clause, and a
    # change of heart after it withdraws it.
    ('wrong song, To The Moon, not To Be With You', 'To Be With You', False),
    ('wrong song, To The Moon, not To Be With You', 'To The Moon', True),
    ('i meant to skip To The Moon', 'To The Moon', False),
    ('wrong song, Love Story, actually no', 'Love Story', False),
    ('wrong song, Love Story... i changed my mind', 'Love Story', False),
    ('not that one, Love Story, no wait', 'Love Story', False),
    ('actually no, wrong song, Love Story', 'Love Story', True),
    ('not that one, Love Story by Taylor Swift', 'Love Story Taylor Swift', True),
    ('not that one, Love Story by Taylor Swift', 'Taylor Swift', False),
    ('wrong song, "Actually No" by someone', 'Actually No', True),
    (FIX, 'join us for a bite', True),
    ('fri not that one lol, Join Us For A Bite', 'join us for a bite', True),
    ('wrong song, "join us for a bite"', 'join us for a bite', True),  # Quoted song names are still theirs.
    ('i meant join us for a bite', 'join us for a bite', True),
    ('nah not that one \u2014 join us for a bite', 'join us for a bite', True),
    ('<@99> not that one, I Will Always Love You', 'I Will Always Love You', True),
    # Sol r2 S4: titles may use any words.
    ('wrong song, i meant Can You Feel My Heart', 'Can You Feel My Heart', True),
    ('wrong song, i meant All I Got by Said The Sky', 'All I Got by Said The Sky', True),
    ('wrong song, The One', 'The One', True),
    ('join us for a bite', 'join us for a bite', False),  # No correction: a plain title isn't a request.
    (FIX, 'someone else song', False),  # Not in their words.
    (FIX, '', False),
    ('for example: not that one, i meant join us for a bite', 'join us for a bite', False),
    ('"not that one, i meant join us for a bite" is what he said', 'join us for a bite', False),
    ("not that one, don't play join us for a bite", 'join us for a bite', False),
    ('join us for a bite, not that one', 'join us for a bite', False),  # The song comes after the correction.
    # Sol r1 and r2 S1: someone else's words in quotes, code or a blockquote never supply the song.
    ('not that one\n> alex: Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('not that one\n```Never Gonna Give You Up```', 'Never Gonna Give You Up', False),
    ('not that one, ``Never Gonna Give You Up``', 'Never Gonna Give You Up', False),
    ('not that one\n>>> alex said\nNever Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('"wrong song, Never Gonna Give You Up"\nnot that one, i meant Join Us For A Bite', 'Never Gonna Give You Up', False),
    # Sol r1 and r2 S2: chat that names no replacement, reported speech and questions aren't requests.
    ('i meant to say thanks', 'thanks', False),
    ('not that one, i meant to say thanks', 'thanks', False),
    ('not the one i wanted lol', 'the one', False),
    ('not the one i wanted lol', 'i wanted', False),
    ('he said not that one, i meant join us for a bite', 'join us for a bite', False),
    ('could you tell me if not that one, i meant join us for a bite works', 'join us for a bite', False),
    # gene #72 (1): the link is consumed once, and negation or cancellation outside a quoted title refuses.
    ("not that one, i meant don't play join us for a bite", 'join us for a bite', False),
    ('not that one, join us for a bite. do not queue anything yet', 'join us for a bite', False),
    ('wrong song, i meant join us for a bite, actually nvm', 'join us for a bite', False),
    ("wrong song, Don't Stop Believin'", "Don't Stop Believin'", False),
    ('wrong song, "Don\'t Stop Believin\'"', "Don't Stop Believin'", True),
    ("wrong song, I Don't Care", "I Don't Care", True),
    ('wrong song, Never Gonna Give You Up', 'Never Gonna Give You Up', True),
    # gene #72 (2): the song is the replacement right after the correction, never someone else's words or
    # an example, though a quoted title still counts.
    ('not that one, alex said "Never Gonna Give You Up"', 'Never Gonna Give You Up', False),
    ('not that one, for example Never Gonna Give You Up', 'Never Gonna Give You Up', False),
    ('not that one, alex said never gonna give you up', 'alex said never gonna give you up', False),
    ('not that one, e.g. never gonna give you up', 'e.g. never gonna give you up', False),
    ('not that one, join us for a bite. alex wants never gonna give you up', 'never gonna give you up', False),
    ('wrong song, join us for a bite, not never gonna give you up', 'never gonna give you up', False),
    ('wrong song, join us for a bite, not never gonna give you up', 'join us for a bite', True),
    ('wrong song, "Alex Said Hi"', 'Alex Said Hi', True),
    ('not that one, i said join us for a bite', 'join us for a bite', True),
    ('not that one, play That Funky Music', 'That Funky Music', True),
    ('not that one, play That Funky Music', 'Play That Funky Music', True),
    # gene #72 (3): only a conversational "i meant to say ..." is an infinitive; "To ..." can start a title.
    ('wrong song, To The Moon', 'To The Moon', True),
    ('not that one, i meant To The Moon', 'To The Moon', True),
    ('wrong song, i meant to say To The Moon', 'To The Moon', False),
    ('not that one, i meant to.', 'to', False),
    # gene #107 (1): after "i meant to", only a word that can't start an infinitive may start a title, whatever
    # the proposed song.
    ('i meant to skip To The Moon', 'to skip To The Moon', False),
    ('not that one, i meant to pause To The Moon', 'to pause To The Moon', False),
    ('wrong song, i meant to tag alex, To The Moon', 'to tag alex To The Moon', False),
    ('not that one, i meant To My Love', 'To My Love', True),
    ('not that one, i meant To Love Somebody', 'To Love Somebody', False),  # Fails closed; these two still count:
    ('wrong song, To Love Somebody', 'To Love Somebody', True),
    ("not that one, i meant 'To Love Somebody'", 'To Love Somebody', True),
    # gene #107 (2): more ways to take the replacement back.
    ('wrong song, Love Story, not that one either', 'Love Story', False),
    ("wrong song, Love Story, that's not it either", 'Love Story', False),
    ('wrong song, Love Story, nope', 'Love Story', False),
    ('wrong song, Love Story nope', 'Love Story', False),
    ('not that one, Love Story... nah', 'Love Story', False),
    ('wrong song, Love Story, no', 'Love Story', False),
    ('wrong song, "Not That One Either"', 'Not That One Either', True),
    ('wrong song, Hell No', 'Hell No', True),
    # gene #107 (3): connectors are skipped only after the title's first word.
    ('wrong song, From Me to You', 'Me to You', False),
    ('wrong song, From Me to You', 'From Me to You', True),
    ('not that one, By The Way', 'The Way', False),
    ('not that one, By The Way', 'By The Way', True),
    ('not that one, play From Me to You', 'Me to You', False),
    # gene #107 (4): single quotes, straight or curly, pair like double quotes; an apostrophe inside a word, or
    # leading one in "'til" or "'93", never opens or closes one.
    ('wrong song, \u2018Actually No\u2019', 'Actually No', True),
    ("wrong song, 'Actually No'", 'Actually No', True),
    ("wrong song, 'Not That One Either' by someone", 'Not That One Either', True),
    ("wrong song, 'Don't Stop Believin''", "Don't Stop Believin'", True),
    ('wrong song, \u2018Don\u2019t Stop Believin\u2019\u2019', "Don't Stop Believin'", True),
    ("wrong song, it's Love Story, don't play it", 'Love Story', False),
    ("not that one, 'Til I Collapse, don't play rockin' robin", "'Til I Collapse", False),
    ("not that one, '93 Til Infinity, don't play rockin' robin", '93 Til Infinity', False),
    ("wrong song, 'Til I Collapse", "'Til I Collapse", True),
    ("not that one, alex said 'Never Gonna Give You Up'", 'Never Gonna Give You Up', False),
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


def test_fri_hears_whether_a_correction_would_find_a_play_even_past_three_reads(store, monkeypatch):
    """Sol r1 S3: the three receipts can all be reads, so the controller says whether one would count."""
    controls = Controls(store)
    assert not controls.correctable('20', '123', 'public:1')
    played(store)
    for action, text in (('queue', 'fri show the queue'), ('now_playing', "fri what's playing"), ('status', 'fri gene status')):
        with store.connection() as db:
            db.execute('UPDATE gene_controls SET created=created-5')
        controls.enqueue(control_body(**{'action': action, 'argument': '', 'request_text': text,
            'source': {**control_body()['source'], 'id': str(discord.utils.time_snowflake(
                dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=30-len(action))))}}))
        claim = controls.poll([action])
        controls.finish(claim['id'], claim['lease'], {'output': 'ok'})
    assert [r['action'] for r in controls.recent('20', '123', 'public:1')] == ['status', 'now_playing', 'queue']
    assert controls.correctable('20', '123', 'public:1')
    assert not controls.correctable('20', '123', 'public:2')
