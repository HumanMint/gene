"""gene #203: a message is read for notes before the write lock is taken, so a long one holds no other write up."""
import sqlite3
from dataclasses import replace

import gene.fri_agent_store as agent_store_module
import gene.store as store_module
from gene.fri_agent_store import AgentStore, snowflake_floor
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)


def probing(path, real, seen):
    """personal_note, recording whether another connection could take the write lock while it ran."""
    def checked(candidate, text):
        probe = sqlite3.connect(path, timeout=0)
        try:
            probe.execute('BEGIN IMMEDIATE')
            probe.execute('ROLLBACK')
            seen.append('free')
        except sqlite3.OperationalError:
            seen.append('locked')
        finally:
            probe.close()
        return real(candidate, text)
    return checked


def test_a_chat_save_reads_its_notes_before_taking_the_write_lock(store, monkeypatch):
    seen = []
    monkeypatch.setattr(store_module, 'personal_note', probing(store.path, store_module.personal_note, seen))
    store.save_chat('guild', 'public:1', 'one', 0, 'i like chess. i love jazz', 'reply', 'model', 'high',
                    notes=[{'category': 'games', 'evidence': 'i like chess'},
                           {'category': 'music', 'evidence': 'i love jazz'},
                           {'category': 'games', 'evidence': 'i like go'}])
    assert seen == ['free', 'free', 'free']
    notes = store.chat_context('guild', 'public:1', 'one')['personal_memory']['notes']
    assert sorted(note['evidence'] for note in notes) == ['i like chess', 'i love jazz']


def test_a_learning_finish_reads_its_notes_before_taking_the_write_lock(learning, monkeypatch):  # noqa: F811
    clock, store, settings, _ = learning
    started = clock[0]-7200
    scheduler = AgentStore(store, replace(settings, agent_history_channels='1'))
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'cursors', {'1': {'before': '100', 'done_at': clock[0]-3600}})
    claim = scheduler.claim()
    source_id = str(int(snowflake_floor(started+600))+1)
    records = [{'id': source_id, 'user_id': '11', 'role_id': '90', 'revision': 0, 'text': 'I love trance',
                'at': started+600}]
    result = {'summary': 'Music chat', 'notes': [{'source_id': source_id, 'category': 'music',
                                                  'evidence': 'I love trance'}]}
    seen = []
    monkeypatch.setattr(agent_store_module, 'personal_note',
                        probing(store.path, agent_store_module.personal_note, seen))
    assert scheduler.finish(claim['id'], records, result, 1, source_id, False, {}, newest_id=source_id)['saved'] == 1
    assert seen == ['free']
    notes = store.chat_context('123', 'public:1', '11')['personal_memory']['notes']
    assert [note['evidence'] for note in notes] == ['I love trance']
