"""A current direct request, rather than matching discussion, authorizes cancellation."""


import pytest

from gene.fri_social import cancellation_requested
from tests.test_fri_social import DM, api, confirmed  # noqa: F401

DISCUSSION = [
    "fri don't cancel my reminder; just explain that reply",
    'fri do not delete my reminder',
    'fri never remove that reminder',
    'fri can you cancel reminders?',
    'fri can you delete reminders?',
    'fri could you cancel a reminder?',
    'fri how do I cancel my reminder?',
    'fri explain how to cancel my reminder',
    'fri if I asked you to cancel my reminder, what would happen?',
    'fri can you cancel my reminder if I ask later?',
    'fri hypothetically cancel my reminder',
    'fri imagine you cancel my reminder',
    'fri cancel my reminder is just an example',
    'fri they said to cancel my reminder',
    'fri "cancel my reminder"',
    'fri “cancel my reminder”',
    "fri 'cancel my reminder'",
    'fri `cancel my reminder`',
    'fri ```cancel my reminder```',
    '> fri cancel my reminder',
    'fri explain this:\n> cancel my reminder',
    'fri https://example.invalid/cancel-my-reminder',
    'fri explain "what is this? also cancel my reminder"',
]
REQUESTS = [
    'fri cancel my reminder to update the mods',
    'fri can you cancel my reminder to update the mods?',
    'fri could you please remove that reminder?',
    'fri please delete the mod update reminder',
    'fri cancel it please',
    'fri cancel my reminder "update the mods"',
    '<@99> cancel my reminder please',
    'what is this reminder screenshot? also cancel my reminder to update the mods',
]


@pytest.mark.parametrize('text', DISCUSSION)
def test_discussion_does_not_supply_cancellation_intent(text):
    assert not cancellation_requested(text, 99)


@pytest.mark.parametrize('text', REQUESTS)
def test_direct_owned_cancellation_intent(text):
    assert cancellation_requested(text, 99)
