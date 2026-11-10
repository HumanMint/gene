"""Bounded structural ancestry; additive tables survive older readers and writers."""
import json
import re

CAPABILITY = 'saved-answer-ids'
MAX_ANSWERS = 1000
MAX_DEPTH = 64
MAX_TURNS = 10000
MAX_MESSAGES = 100  # As Fri's ancestry: retain the newest IDs and mark overflow unknown.
UNKNOWN = 'Earlier answer whose full message ancestry is unavailable. Not shown.'


def checked(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ANSWERS:
        return []
    if not all(isinstance(i, str) and re.fullmatch(r'(?:[0-9]{1,25}|turn:[0-9a-f]{32})', i) for i in value):
        return []
    return sorted(set(value))


def retirement_markers():
    # Exact controller-owned lifecycle markers, never matching withdrawn/member words against an answer.
    from .fri_member_summary import RETIRED
    from .store import DRAWN
    return (DRAWN, RETIRED, UNKNOWN)


def initialize(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}
    if 'answer_retired' not in columns:
        db.execute('ALTER TABLE chat_turns ADD COLUMN answer_retired INTEGER NOT NULL DEFAULT 0')
    db.execute('UPDATE chat_turns SET answer_retired=1 WHERE answer_retired=0 AND verbatim=0 '
               'AND reply IN (?,?,?)', retirement_markers())
    if 'answer_messages_unknown' not in columns:
        db.execute('ALTER TABLE chat_turns ADD COLUMN answer_messages_unknown INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE TRIGGER IF NOT EXISTS chat_answer_retired AFTER UPDATE OF reply ON chat_turns BEGIN '
               'UPDATE chat_turns SET answer_retired=1 WHERE id=NEW.id; END')
    if 'answer_key' not in columns:
        db.execute('ALTER TABLE chat_turns ADD COLUMN answer_key TEXT')
    db.execute("UPDATE chat_turns SET answer_key='turn:'||lower(hex(randomblob(16))) WHERE answer_key IS NULL")
    db.execute("CREATE TRIGGER IF NOT EXISTS chat_answer_key AFTER INSERT ON chat_turns "
               "WHEN NEW.answer_key IS NULL BEGIN UPDATE chat_turns SET answer_key='turn:'||lower(hex(randomblob(16))) "
               "WHERE id=NEW.id; END")
    db.execute('CREATE INDEX IF NOT EXISTS chat_turns_scope ON chat_turns(guild_id,user_id,channel_id)')
    db.execute("CREATE INDEX IF NOT EXISTS chat_answer_identity ON chat_turns(guild_id,coalesce(reply_message_id,answer_key))")
    db.execute('CREATE TABLE IF NOT EXISTS chat_answer_ancestors ('
               'guild_id TEXT NOT NULL, turn_id INTEGER NOT NULL, answer_id TEXT NOT NULL, depth INTEGER NOT NULL,'
               'PRIMARY KEY(guild_id,answer_id,turn_id))')
    db.execute('CREATE INDEX IF NOT EXISTS chat_answer_turn ON chat_answer_ancestors(turn_id)')
    # Older controllers delete and trim turns too. No foreign-key pragma dependency.
    db.execute('CREATE TRIGGER IF NOT EXISTS chat_answer_cleanup AFTER DELETE ON chat_turns BEGIN '
               'DELETE FROM chat_answer_ancestors WHERE turn_id=OLD.id; END')

    db.execute('CREATE TABLE IF NOT EXISTS chat_drawn_messages ('
               'guild_id TEXT NOT NULL,message_id TEXT NOT NULL,turn_id INTEGER NOT NULL,'
               'PRIMARY KEY(guild_id,message_id,turn_id))')
    db.execute('CREATE INDEX IF NOT EXISTS chat_drawn_turn ON chat_drawn_messages(turn_id)')
    for event in ('INSERT', 'UPDATE OF drew_on'):
        name = 'insert' if event == 'INSERT' else 'update'
        db.execute(f'CREATE TRIGGER IF NOT EXISTS chat_drawn_{name} AFTER {event} ON chat_turns BEGIN '
                   'DELETE FROM chat_drawn_messages WHERE turn_id=NEW.id; '
                   'INSERT OR IGNORE INTO chat_drawn_messages '
                   "SELECT NEW.guild_id,value,NEW.id FROM json_each(CASE WHEN json_valid(NEW.drew_on) "
                   "THEN NEW.drew_on ELSE '[]' END) WHERE type='text'; END")
    db.execute('CREATE TRIGGER IF NOT EXISTS chat_drawn_cleanup AFTER DELETE ON chat_turns BEGIN '
               'DELETE FROM chat_drawn_messages WHERE turn_id=OLD.id; END')
    # Once, including pre-contract saves; triggers keep old-controller writes indexed during rollback.
    if db.execute("SELECT 1 FROM settings WHERE key='saved_answers_indexed'").fetchone() is None:
        db.execute('INSERT OR IGNORE INTO chat_drawn_messages SELECT t.guild_id,j.value,t.id '
                   "FROM chat_turns t,json_each(CASE WHEN json_valid(t.drew_on) THEN t.drew_on ELSE '[]' END) j "
                   "WHERE j.type='text'")
        db.execute("INSERT INTO settings(key,value) VALUES ('saved_answers_indexed','true')")

    db.execute('CREATE TABLE IF NOT EXISTS chat_answer_messages ('
               'guild_id TEXT NOT NULL,message_id TEXT NOT NULL,turn_id INTEGER NOT NULL,'
               'PRIMARY KEY(guild_id,message_id,turn_id))')
    db.execute('CREATE INDEX IF NOT EXISTS chat_answer_message_turn ON chat_answer_messages(turn_id)')
    db.execute('CREATE TRIGGER IF NOT EXISTS chat_answer_message_cleanup AFTER DELETE ON chat_turns BEGIN '
               'DELETE FROM chat_answer_messages WHERE turn_id=OLD.id; END')


def inherited_messages(db, guild, value):
    """Only actual carried answers contribute; a question's source ID cannot start this inheritance."""
    found, unknown = set(), False
    for answer in checked(value):
        for parent in db.execute('SELECT id,source,burst,answer_messages_unknown FROM chat_turns '
                                 'WHERE guild_id=? AND coalesce(reply_message_id,answer_key)=?', (guild, answer)):
            unknown |= bool(parent['answer_messages_unknown'])
            source = json.loads(parent['source'] or '{}')
            sources = [source, *(item.get('source') or {} for item in json.loads(parent['burst'] or '[]'))]
            found.update(str(item['id']) for item in sources if str(item.get('id', '')).isdigit())
            found.update(row[0] for row in db.execute(
                'SELECT message_id FROM chat_drawn_messages WHERE turn_id=? UNION '
                'SELECT message_id FROM chat_answer_messages WHERE turn_id=?', (parent['id'], parent['id'])))
            if len(found) > MAX_MESSAGES:
                unknown = True
                found = set(sorted(found, key=int, reverse=True)[:MAX_MESSAGES])
    return found, unknown


def ancestors(db, guild, value):
    """None means no exchange can safely be kept within the contract's bounds."""
    answers = checked(value)
    if not answers:
        return {}
    # Cap tracked turns as well as fan-in and depth: a withdrawal reads at most MAX_TURNS descendants.
    if len(db.execute('SELECT DISTINCT turn_id FROM chat_answer_ancestors WHERE guild_id=? LIMIT ?',
                      (guild, MAX_TURNS)).fetchall()) >= MAX_TURNS:
        return None
    found = dict.fromkeys(answers, 1)
    for answer in answers:
        for row in db.execute("SELECT a.answer_id,a.depth FROM chat_turns t JOIN chat_answer_ancestors a "
                              "ON a.turn_id=t.id WHERE t.guild_id=? AND coalesce(t.reply_message_id,t.answer_key)=?",
                              (guild, answer)):
            found[row['answer_id']] = max(found.get(row['answer_id'], 0), row['depth'] + 1)
            if len(found) > MAX_ANSWERS or found[row['answer_id']] > MAX_DEPTH:
                return None
    return found
