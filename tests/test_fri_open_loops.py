"""Fri sees the speaker's own unfinished things, only in the conversation each one already shows in."""

import time

from gene.fri_social_store import Social
from gene.fri_workspace_store import Workspace
from tests.test_fri_social import (
    DM,
    PUBLIC,
    confirmed,
    due,
    future,
    plan,
    published,
    reminder,
    source,
)
from tests.test_fri_workspace import source as work_source

DM_EMPTY = {'reminders': [], 'saved_work': [], 'game_nights': None}
CHANNEL_EMPTY = {'reminders': None, 'saved_work': [], 'game_nights': []}


def loops(store, conversation='private:999', user='10'):
    return store.chat_context('123', conversation, user)['loops']


def test_reminders_show_only_in_the_dm_they_were_made_in(store):
    social = Social(store)
    preview = reminder(social)
    [row] = loops(store)['reminders']
    assert row['state'] == 'pending' and row['text'] == 'update the mods' and row['timezone'] == 'UTC'
    assert row['due'] == preview['due'] and row['local_time'] == preview['local_time']
    confirmed(social, 10)
    assert sorted(r['state'] for r in loops(store)['reminders']) == ['pending', 'scheduled']
    # The /fri panel and server channels are other conversations: a DM reminder never shows there.
    assert loops(store, 'private:456')['reminders'] == []
    for conversation in ('public:456', 'public:999'):  # Never visible in a channel, so not even "none".
        assert loops(store, conversation)['reminders'] is None
    assert loops(store, user='11')['reminders'] == []


def test_settled_reminders_stay_a_day_and_cancelled_or_expired_ones_drop(store):
    social = Social(store)
    sent, cancelled, stale = confirmed(social), confirmed(social, 10), reminder(social, 20)
    social.reminder_decide(cancelled['id'], DM, source(30), 0, 'cancel')
    due(store, sent['id'])
    claim = social.reminder_claim()
    social.reminder_sending(claim['id'], claim['lease'])
    social.reminder_finish(claim['id'], claim['lease'], 'sent', '555')
    with store.connection() as db:
        db.execute('UPDATE fri_reminders SET created=? WHERE id=?', (time.time()-901, stale['id']))
    assert [(r['state'], r['text']) for r in loops(store)['reminders']] == [('sent', 'update the mods')]
    with store.connection() as db:
        db.execute('UPDATE fri_reminders SET due=?,lease_until=? WHERE id=?',
                   (time.time()-86500, time.time()-86401, sent['id']))  # A day after it went out.
    assert loops(store)['reminders'] == []


def test_a_reminder_a_privacy_change_will_stop_is_not_called_scheduled(store):
    social = Social(store)
    confirmed(social)
    reminder(social, 10)
    store.clear_chats('10')  # Delivery checks the revision it was confirmed under, so this one never goes out.
    assert [r['state'] for r in loops(store)['reminders']] == ['will_not_send', 'will_not_send']
    claim = social.reminder_claim()
    assert claim is None


def test_open_saved_work_shows_only_in_its_own_conversation(store):
    work = Workspace(store)
    draft = work.save(DM, work_source(), 0, '', 0, 'Raid night intro', 'the intro text', 'open')
    done = work.save(DM, work_source(1), 0, '', 0, 'Old checklist', 'checklist', 'open')
    work.save(DM, work_source(2), 0, done['id'], 1, 'Old checklist', 'checklist', 'completed')
    [row] = loops(store)['saved_work']
    assert row == {'title': 'Raid night intro', 'updated': draft['updated']}
    for conversation in ('public:999', 'private:456'):
        assert loops(store, conversation)['saved_work'] == []
    assert loops(store, user='11')['saved_work'] == []


def test_game_nights_show_the_organizers_cards_in_their_channel_without_anyones_votes(store):
    social = Social(store)
    card = published(social)
    social.plan_decide(card['id'], {**PUBLIC, 'user_id': '11'}, source(2, interaction=True, public=True, user='11'),
                       0, 'vote1', '777')
    [row] = loops(store, 'public:456')['game_nights']
    assert row['title'] == 'Helldivers' and row['state'] == 'open' and row['chosen'] is None
    assert row['options'] == card['slots'] and row['available'] == [0, 1] and row['rsvps'] is None
    assert set(row) == {'title', 'state', 'timezone', 'options', 'available', 'chosen', 'rsvps'}
    # A member who only voted doesn't get the card as theirs, and it never leaves its channel.
    assert loops(store, 'public:456', user='11')['game_nights'] == []
    assert loops(store, 'public:457')['game_nights'] == []
    for conversation in ('private:999', 'private:456'):
        assert loops(store, conversation)['game_nights'] is None
    social.plan_decide(card['id'], PUBLIC, source(3, interaction=True, public=True), 0, 'final1', '777')
    social.plan_decide(card['id'], {**PUBLIC, 'user_id': '11'}, source(4, interaction=True, public=True, user='11'),
                       0, 'going', '777')
    [row] = loops(store, 'public:456')['game_nights']
    assert row['chosen'] == card['slots'][1] and row['rsvps'] == {'going': 1, 'maybe': 0, 'cannot': 0}


def test_a_game_night_that_already_happened_or_was_cancelled_drops(store):
    social = Social(store)
    card = published(social)
    pending = plan(social, 10)  # Not published yet: still the organizer's to finish.
    assert [row['state'] for row in loops(store, 'public:456')['game_nights']] == ['pending', 'open']
    social.bind('plan', pending['id'], PUBLIC, '778')
    social.plan_decide(pending['id'], PUBLIC, source(11, interaction=True, public=True), 0, 'cancel', '778')
    with store.connection() as db:
        db.execute('UPDATE fri_plans SET slots=? WHERE id=?', (f'[{time.time()-4*3600}, {time.time()-3*3600-60}]', card['id']))
    assert loops(store, 'public:456')['game_nights'] == []


def test_memory_off_shows_nothing_and_forget_me_erases_it(store):
    social = Social(store)
    confirmed(social)
    published(social)
    Workspace(store).save(DM, work_source(), 0, '', 0, 'Raid night intro', 'the intro text', 'open')
    assert loops(store)['reminders'] and loops(store)['saved_work'] and loops(store, 'public:456')['game_nights']
    store.personal_control('10', 'disable')
    assert loops(store) == DM_EMPTY and loops(store, 'public:456') == CHANNEL_EMPTY
    store.personal_control('10', 'enable')
    assert loops(store) == DM_EMPTY and loops(store, 'public:456') == CHANNEL_EMPTY


def test_the_api_returns_loops_for_the_speakers_conversation(store):
    social = Social(store)
    social.reminder_prepare(DM, source(), 0, 'stretch', future(2), 'UTC')
    context = store.chat_context('123', 'private:999', '10', False)  # An older Fri gets them too and ignores them.
    assert [row['text'] for row in context['loops']['reminders']] == ['stretch']


def test_a_late_delivery_stays_visible_for_a_day_after_it_settles(store):
    social = Social(store)
    late = confirmed(social)
    with store.connection() as db:  # Fri was down for days: it goes out long after it was due.
        db.execute('UPDATE fri_reminders SET due=? WHERE id=?', (time.time()-3*86400, late['id']))
    assert [r['state'] for r in loops(store)['reminders']] == ['scheduled']
    claim = social.reminder_claim()
    social.reminder_sending(claim['id'], claim['lease'])
    social.reminder_finish(claim['id'], claim['lease'], 'sent', '555')
    assert [r['state'] for r in loops(store)['reminders']] == ['sent']
