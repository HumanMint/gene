import time

import pytest

from gene.fri_history_search import MAX_DAYS, bounds, requested, valid_action


def query(**changes):
    return {'kind': 'history_search', 'channel_id': '1', 'query': 'played', 'author': '',
            'after': '', 'before': '', 'cursor': '', **changes}


def test_archive_intent_and_date_limits_are_bounded():
    for text in ('what was that game Dipper recommended last month?', 'search chat for watched',
                 'find messages before 2026-01-01', 'look through history for Minecraft',
                 'what did Dipper recommend?', 'what did I say about Minecraft?',
                 'find that game Dipper recommended last month', 'who recommended Minecraft?',
                 'what did we talk about?', 'what did Dipper mention in chat?'):
        assert requested(text)
    for text in ('hey fri', 'play Sad Machine', 'do not search chat', '> find old messages',
                 'what do you recommend?', 'what should I say to a friend who is upset?',
                 'which game would you recommend?', 'what do I have to say?',
                 'find recommended movies', 'look up recommended games',
                 'what are the recommended settings for this game?',
                 'what is the recommended way to learn Python?'):
        assert not requested(text)
    assert valid_action(query())
    assert not valid_action(query(cursor='invented'))
    assert not valid_action(query(query='x'*81))
    now = time.time()
    assert bounds('', '', now) == (now-MAX_DAYS*86400, now)
    with pytest.raises(ValueError):
        bounds('2026-99-99', '', now)
    with pytest.raises(ValueError):
        bounds('2026-02-01', '2026-01-01', now)
