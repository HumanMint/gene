"""Fri knows which of the speaker's own requests are still open, without exposing their text in public."""

import time

from tests.test_api import headers

ASH = {'user_id': '30', 'revision': 0}


def submit(store, user='20', source='draft-1', body='Add a modpack checklist\nwith more detail', provenance=()):
    return store.request('123', user, source, '', body, list(provenance))


def requests(store, conversation='private:9', user='20'):
    return store.chat_context('123', conversation, user)['requests']


def test_only_the_speakers_open_requests_are_listed(store):
    own = submit(store)
    submit(store, user='40', source='other')
    listed = requests(store)
    assert [row['id'] for row in listed] == [own['id']]
    assert listed[0]['state'] == 'awaiting_owner' and listed[0]['kind'] == 'feature'


def test_titles_appear_only_in_dms_and_never_for_derived_drafts(store):
    own, derived = submit(store), submit(store, source='draft-2', body='Build what ash described', provenance=[ASH])
    private = {row['id']: row for row in requests(store)}
    assert private[own['id']]['title'] == 'Add a modpack checklist'
    assert 'title' not in private[derived['id']]
    assert all('title' not in row for row in requests(store, 'public:1'))


def test_settled_requests_drop_out_after_a_week_and_memory_off_hides_all(store):
    open_job, settled = submit(store), submit(store, source='draft-2')
    with store.connection() as db:
        db.execute("UPDATE jobs SET state='rejected',updated=? WHERE id=?", (time.time()-8*86400, settled['id']))
    assert [row['id'] for row in requests(store)] == [open_job['id']]
    with store.connection() as db:
        db.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time()-86400, settled['id']))
    assert len(requests(store)) == 2
    store.personal_control('20', 'disable')
    assert requests(store) == []


def test_erased_requests_are_not_attributed_to_anyone(store):
    submit(store)
    store.erase_user('20')
    store.personal_control('20', 'enable')
    assert requests(store) == []


def test_requests_without_known_attribution_never_show_a_title(store, client):
    legacy = store.request('123', '20', 'legacy', '', 'Quote what ash said in a feature')  # No provenance sent.
    assert 'title' not in requests(store)[0] and requests(store)[0]['id'] == legacy['id']
    payload = {'guild_id': 'guild', 'user_id': 'member', 'source_id': 'panel-1', 'source_url': '',
               'body': 'Add a modpack checklist', 'channel_id': 'workshop'}
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 200
    assert client.post('/requests', headers=headers('fri'), json={**payload, 'source_id': 'panel-2', 'provenance': []}).status_code == 200
    with client.app.state.store.connection() as db:
        assert [row[0] for row in db.execute('SELECT attributed FROM jobs ORDER BY created')] == [0, 1]


def test_a_request_withdrawn_today_stays_listed_even_if_it_was_old(store):
    old = submit(store, provenance=[ASH])
    with store.connection() as db:
        db.execute('UPDATE jobs SET created=?,updated=? WHERE id=?', (time.time()-8*86400, time.time()-8*86400, old['id']))
    store.personal_control('30', 'disable')  # Withdrawn now: it should still say so for a week.
    listed = requests(store)
    assert [(row['id'], row['state']) for row in listed] == [(old['id'], 'rejected')]
