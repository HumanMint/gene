"""Which version of a Discord message each saved exchange and note was made from, on Discord's own clock
(HumanMint/fri#358 E1): its edit time, or its creation time if it was never edited, as Fri saw it when saving.

An edit withdrawn by ID comes with its time, also on Discord's clock, and is compared with these, never with when the
controller saved anything: the controller's clock may run seconds apart from Discord's, either way (Sol round 1 of
fri #364). A later version took away what the save holds, so it goes. The same version again is a pin or an embed
refresh repeating that edit's time, and an earlier one is an edit's late event: nothing of the save goes. A save that
recorded none (from an older Fri, or made before this) can't be placed: it goes as before while its message is young,
and is held back by Fri otherwise.

Kept in a table of its own, so an older controller works on the same database as before: triggers clear what an
exchange or a note no longer holds, whichever controller deletes or saves it again.
"""
import json
import math

TABLE = ('CREATE TABLE IF NOT EXISTS fri_source_versions (kind TEXT NOT NULL, item INTEGER NOT NULL, '
         'message_id TEXT NOT NULL, version REAL NOT NULL, PRIMARY KEY(kind, item, message_id))')
TRIGGERS = {
    'fri_source_versions_turn_gone':
        "AFTER DELETE ON chat_turns BEGIN DELETE FROM fri_source_versions WHERE kind='turn' AND item=OLD.id; END",
    'fri_source_versions_note_gone':
        "AFTER DELETE ON personal_notes BEGIN DELETE FROM fri_source_versions WHERE kind='note' AND item=OLD.id; END",
    # A note saved again (sources_at moves with every save) holds what the new save says, not what it held before.
    'fri_source_versions_note_saved': "AFTER UPDATE OF sources_at ON personal_notes BEGIN DELETE FROM "
                                      "fri_source_versions WHERE kind='note' AND item=OLD.id; END",
}
YOUNG = 90*86400  # As long as a note is kept, as Fri's queue asks about an edit with no time (fri #356).
SAME = 0.001  # Seconds: one edit's time, read twice.
EPOCH = 1420070400  # Discord's, for a message ID's own time.


def initialize(db):
    db.execute(TABLE)
    for name, body in TRIGGERS.items():
        db.execute(f'CREATE TRIGGER IF NOT EXISTS {name} {body}')


def valid(versions):
    """Of the versions a save came with (message ID -> Discord time), those that can be recorded."""
    return {str(key): float(value) for key, value in (versions or {}).items()
            if str(key).isdigit() and type(value) in (int, float) and math.isfinite(value) and value > 0}


def record(db, kind, item, versions, messages):
    """The versions of the messages an exchange (kind 'turn') or a note ('note') holds, as its save said them."""
    for message in sorted({str(message) for message in messages} & versions.keys()):
        db.execute('INSERT INTO fri_source_versions VALUES (?,?,?,?) ON CONFLICT(kind,item,message_id) DO UPDATE SET '
                   'version=excluded.version', (kind, item, message, versions[message]))


def record_note(db, guild, scope, user, note, versions, messages):
    """As record, for the note just saved under that key (see fri_memory_store.save)."""
    row = db.execute('SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? AND category=? '
                     'AND evidence=?', (guild, scope, user, note[0], note[1])).fetchone()
    if row:
        record(db, 'note', row[0], versions, messages)


def holding(db, guild, message):
    """Each exchange and current note holding the message: (whose, the version it was saved from, or None)."""
    from .fri_memory_store import CURRENT
    turns = db.execute(
        "SELECT t.user_id,v.version FROM chat_turns t LEFT JOIN fri_source_versions v ON v.kind='turn' AND v.item=t.id "
        "AND v.message_id=? WHERE t.guild_id=? AND (json_extract(NULLIF(t.source,''),'$.id')=? OR EXISTS (SELECT 1 FROM "
        "json_each(t.burst) b WHERE json_extract(b.value,'$.source.id')=?))", (message, guild, message, message))
    notes = db.execute(
        f"SELECT n.user_id,v.version FROM personal_notes n LEFT JOIN fri_source_versions v ON v.kind='note' AND "
        f"v.item=n.id AND v.message_id=? WHERE n.guild_id=? AND {CURRENT} AND instr(n.sources,?)>0",
        (message, guild, json.dumps(message)))
    return [(row[0], row[1]) for row in [*turns, *notes]]


def place(db, guild, message, edited_at, now):
    """How an edit of the message at `edited_at` (Discord's time) stands against what holds it: 'withdraw' when every
    save holding it was made from an earlier version (or nothing holds it), 'keep' when every one was made from that
    version or a later one, and 'unsure' otherwise: a save of each kind (a withdrawal by ID takes them all), or one
    that recorded no version of a message older than YOUNG. One with no version of a younger message goes, as before.
    Returns that and the members whose saves hold it."""
    young = (int(message) >> 22)/1000+EPOCH > now-YOUNG
    kinds, users = set(), set()
    for user, version in holding(db, guild, message):
        users.add(user)
        if version is None:
            kinds.add('withdraw' if young else 'unsure')
        else:
            kinds.add('withdraw' if edited_at > version+SAME else 'keep')
    if kinds <= {'withdraw'}:
        return 'withdraw', users
    return ('keep' if kinds == {'keep'} else 'unsure'), users
