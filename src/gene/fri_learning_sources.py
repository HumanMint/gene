"""Which Discord messages each learning report and channel summary read (HumanMint/gene#244), so a message taken back
takes their words with it.

A learning pass reports to the owner: the summary it made and each preference it saved, word for word. Its card repeats
the summary, and the channel summary it saves reaches Fri's context in that channel and the owner's learning tool, as
do an archive search's observations, each with its source's exact words. A message the pass read, withdrawn later
(edited or deleted, by ID or by its exchange's scope), leaves none of that: the report and its card keep a wordless
breadcrumb, pending or delivered, the channel summary goes, and so does an observation from it. So does everything of a
channel withdrawn as a whole (gene #243).

Each report and summary records the messages it read, by ID only, in a table of its own, so an older controller works on
the same database as before. Triggers clear a record with its report or summary, whichever controller deletes or
replaces it. One with no record (from before this, or saved by an older controller since) doesn't say what it read, so
it's never destroyed for a message it may not have read (Sol round 1 of #247): it's held back instead, shown and
delivered only as a wordless placeholder and used nowhere, its words kept until it expires or is replaced. And only
where the message may have been said: a withdrawal by an exchange's scope names its conversation, so only a report or
summary of that channel (none for a DM); one by ID names no channel, so any whose time could hold it. A report could
hold it if the message was sent within the range its card says it examined (or it has no card), a summary if it was sent
before the summary was made. Learning reads a channel's own history, which a thread's messages aren't part of, so a
thread's parent never needs matching.

The words held back are set aside, in a table of their own, and the placeholder is what the report, card and summary
tables hold, so an older controller, which reads those, shows none of them either (gene #252). They go with the report or
summary, by triggers, whichever controller deletes or replaces it. A report's words and card last a week, delivered or
not (gene #250): see expire.
"""
import json
import re

TABLE = ('CREATE TABLE IF NOT EXISTS fri_learning_sources (kind TEXT NOT NULL, item TEXT NOT NULL, '
         'messages TEXT, held INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, item))')
TRIGGERS = {
    'fri_learning_sources_report_gone':
        "AFTER DELETE ON agent_reports BEGIN DELETE FROM fri_learning_sources WHERE kind='report' AND item=OLD.id; END",
    'fri_learning_sources_summary_gone': "AFTER DELETE ON agent_summaries BEGIN DELETE FROM fri_learning_sources "
                                         "WHERE kind='summary' AND item=OLD.channel_id; END",
    # The channel's next summary, from any controller, read what it says, not what this one read.
    'fri_learning_sources_summary_replaced': "AFTER UPDATE ON agent_summaries BEGIN DELETE FROM fri_learning_sources "
                                             "WHERE kind='summary' AND item=OLD.channel_id; END",
    # What a held report or summary said, set aside (see hold), goes with its record: with the report or summary itself
    # (above), its next summary, an erasure, or once it's no longer held, whichever controller does it.
    'fri_learning_held_gone': 'AFTER DELETE ON fri_learning_sources BEGIN DELETE FROM fri_learning_held '
                              'WHERE kind=OLD.kind AND item=OLD.item; END',
    'fri_learning_held_released': 'AFTER UPDATE OF held ON fri_learning_sources WHEN NOT NEW.held BEGIN DELETE FROM '
                                  'fri_learning_held WHERE kind=NEW.kind AND item=NEW.item; END',
}
# The words of a report or summary held back, by kind and item as above: a JSON object of what its row held.
HELD_WORDS = ('CREATE TABLE IF NOT EXISTS fri_learning_held (kind TEXT NOT NULL, item TEXT NOT NULL, kept TEXT NOT NULL, '
              'PRIMARY KEY(kind, item))')
# Held by the mark alone, as before gene #252 (and by such a controller again, after a rollback): set aside at the start.
UNMOVED = ('SELECT kind,item FROM fri_learning_sources s WHERE held AND NOT EXISTS (SELECT 1 FROM fri_learning_held h '
           'WHERE h.kind=s.kind AND h.item=s.item)')
KEPT = 7*86400  # A learning report's week, delivered or not.
SLACK = 2  # Seconds, either way, between a message's own time and the time a pass recorded for it.
EPOCH = 1420070400  # Discord's, for a message ID's own time.
CHANNEL = re.compile(r'Channel: <#(\d+)>')
INTERRUPTED = 'Learning pass interrupted.'  # A report the controller wrote itself, with no member's words.
TAKEN = 'Its findings were removed: a message it read was taken back.'
HELD = 'Held back: a message it may have read was taken back.'
EXPIRED = 'Its findings were removed: it went a week without being delivered.'
MARKS = ('held', 'withdrawn', 'expired')  # What a wordless card says it is.
# A channel summary shown or used: not one held back.
SHOWN_SUMMARY = ("NOT EXISTS (SELECT 1 FROM fri_learning_sources s WHERE s.kind='summary' "
                 "AND s.item=agent_summaries.channel_id AND s.held)")


def initialize(db):
    db.execute(TABLE)
    db.execute(HELD_WORDS)
    for name, body in TRIGGERS.items():
        db.execute(f'CREATE TRIGGER IF NOT EXISTS {name} {body}')
    if db.execute(UNMOVED).fetchone():
        began = not db.in_transaction
        if began:
            db.execute('BEGIN IMMEDIATE')
        for kind, item in db.execute(UNMOVED).fetchall():  # Read again, locked.
            hold(db, kind, item)
        if began:
            db.commit()


def record(db, kind, item, messages):
    """What a report (kind 'report', by its ID) or a channel's summary ('summary', by the channel) read. One held is no
    longer held, and what was set aside for it goes (see TRIGGERS)."""
    db.execute('INSERT INTO fri_learning_sources VALUES (?,?,?,0) ON CONFLICT(kind, item) DO UPDATE SET '
               'messages=excluded.messages,held=0', (kind, str(item), json.dumps(sorted({str(m) for m in messages}))))


def hold(db, kind, item):
    """Held back (see found): what it said is set aside, and the tables every controller reads keep a wordless
    placeholder (gene #252). A report keeps its breadcrumb and its card without its summary, findings or term; a summary
    keeps no words and no revisions, which every controller counts as stale."""
    kept = None
    if kind == 'report':
        row = db.execute('SELECT r.body,c.body AS card FROM agent_reports r LEFT JOIN agent_report_cards c ON '
                         'c.report_id=r.id WHERE r.id=?', (str(item),)).fetchone()
        if row:
            kept, card = {'body': row['body'], 'card': row['card']}, json.loads(row['card']) if row['card'] else {}
            db.execute('UPDATE agent_reports SET body=? WHERE id=?',
                       (breadcrumb(str(item), channel_of(row['body'], card), HELD), str(item)))
            if card:
                db.execute('UPDATE agent_report_cards SET body=? WHERE report_id=?',
                           (json.dumps(wordless(card, held=True)), str(item)))
    else:
        row = db.execute('SELECT body,revisions FROM agent_summaries WHERE channel_id=?', (str(item),)).fetchone()
        if row:
            kept = {'body': row['body'], 'revisions': row['revisions']}
            # First: as its replacement, this clears its record, and what was set aside with it.
            db.execute("UPDATE agent_summaries SET body='',revisions='{}' WHERE channel_id=?", (str(item),))
    if kept is not None:
        db.execute('INSERT INTO fri_learning_held VALUES (?,?,?) ON CONFLICT(kind, item) DO NOTHING',
                   (kind, str(item), json.dumps(kept)))
    db.execute('INSERT INTO fri_learning_sources VALUES (?,?,NULL,1) ON CONFLICT(kind, item) DO UPDATE SET held=1',
               (kind, str(item)))


def expire(db, now):
    """Reports past their week (gene #250): a delivered one goes, with its card and anything set aside for it; one still
    to be delivered keeps only a wordless breadcrumb, pending, so the owner still hears the pass ran (it no longer pauses
    learning, and goes out with the next pass's report: see AgentStore). One with no words (taken back, or an
    interrupted pass's) stays as it is. Inside the caller's transaction, or one its first statement begins."""
    db.execute('DELETE FROM agent_reports WHERE delivered=1 AND at<?', (now-KEPT,))
    for row in db.execute('SELECT r.id,r.body,c.body AS card FROM agent_reports r LEFT JOIN agent_report_cards c ON '
                          'c.report_id=r.id WHERE r.delivered=0 AND r.at<?', (now-KEPT,)).fetchall():
        if row['body'].endswith((TAKEN, EXPIRED)) or row['body'].startswith(INTERRUPTED):
            continue
        card = json.loads(row['card']) if row['card'] else {}
        db.execute('UPDATE agent_reports SET body=? WHERE id=?',
                   (breadcrumb(row['id'], channel_of(row['body'], card), EXPIRED), row['id']))
        if card:
            db.execute('UPDATE agent_report_cards SET body=? WHERE report_id=?',
                       (json.dumps(wordless(card, expired=True)), row['id']))
        record(db, 'report', row['id'], [])  # Nothing of it is left to take.


def sent(message):
    return ((int(message) >> 22)/1000)+EPOCH


def channel_of(body, card):
    named = CHANNEL.search(body)
    return str(card.get('channel_id') or (named[1] if named else ''))


def breadcrumb(report, channel, text):
    return f'Learning pass {report[:8]}\n'+(f'Channel: <#{channel}>\n' if channel.isdigit() else '')+text


def wordless(card, **mark):
    """A card without its words, marked as what it is now (and no longer as what it was)."""
    if not card:
        return {}
    return {**{key: value for key, value in card.items() if key not in MARKS}, 'summary': '', 'findings': [], 'term': '',
            **mark}


def shown(report, body, card, held):
    """A report's body and card as anything shows or delivers them: a held one only as a wordless placeholder."""
    if not held:
        return body, card
    return breadcrumb(report, channel_of(body, card), HELD), wordless(card, held=True)


def found(db, messages=(), channels=(), said_in=None):
    """What these messages, or these whole channels, take: the reports to breadcrumb, those to hold back, the channel
    summaries to drop, those to hold back, and whether an archive observation came from one. said_in: the channel the
    messages were said in, '' for none that learning reads (a DM), None if not known (see above)."""
    messages = {str(message) for message in messages if str(message).isdigit()}
    channels = {str(channel) for channel in channels}
    times = [sent(message) for message in messages]

    def nearby(channel):
        return bool(times) and (said_in is None or (said_in != '' and channel == said_in))
    taken, held = [], []
    for row in db.execute("SELECT r.id,r.body,c.body AS card,s.messages AS read,coalesce(s.held,0) AS held "
                          "FROM agent_reports r LEFT JOIN agent_report_cards c ON c.report_id=r.id LEFT JOIN "
                          "fri_learning_sources s ON s.kind='report' AND s.item=r.id").fetchall():
        card = json.loads(row['card']) if row['card'] else {}
        channel = channel_of(row['body'], card)
        if channel in channels or (row['read'] is not None and messages & set(json.loads(row['read']))):
            taken.append((row['id'], channel, card))
        elif row['read'] is None and not row['held'] and nearby(channel):
            if card:  # Within the range it examined; one that examined none read no member's words.
                low, high = card.get('oldest'), card.get('newest')
                could = (type(low) in (int, float) and type(high) in (int, float)
                         and any(low-SLACK <= at <= high+SLACK for at in times))
            else:
                could = not row['body'].startswith(INTERRUPTED)
            if could:
                held.append(row['id'])
    dropped, kept_back = [], []
    for row in db.execute("SELECT a.channel_id,a.at,s.messages AS read,coalesce(s.held,0) AS held FROM agent_summaries "
                          "a LEFT JOIN fri_learning_sources s ON s.kind='summary' AND s.item=a.channel_id").fetchall():
        if row['channel_id'] in channels or (row['read'] is not None and messages & set(json.loads(row['read']))):
            dropped.append(row['channel_id'])
        elif (row['read'] is None and not row['held'] and nearby(row['channel_id'])
              and any(at <= row['at']+SLACK for at in times)):
            kept_back.append(row['channel_id'])
    marks = ','.join('?'*len(messages))
    observed = bool((messages or channels) and db.execute(
        f'SELECT 1 FROM fri_explore_notes WHERE source_id IN ({marks}) OR channel_id IN '
        f"({','.join('?'*len(channels))}) LIMIT 1", (*sorted(messages), *sorted(channels))).fetchone())
    return taken, held, dropped, kept_back, observed


def naming(db, messages=(), channels=(), said_in=None):
    """Whether withdrawing these takes anything (see withdraw): a withdrawal only takes the write lock then."""
    return any(found(db, messages, channels, said_in))


def withdraw(db, messages=(), channels=(), said_in=None):
    """Messages, or whole channels, taken back: the learning reports that read them keep only a breadcrumb (no
    summary, disclosures or findings), as do their cards, pending or delivered; the channel summaries that read them
    go, and so do the archive observations made from them. A report or summary with no record of what it read is held
    back instead, where it may have read them (see found). Returns whether anything was taken or held. Inside the
    caller's write transaction."""
    taken, held, dropped, kept_back, observed = found(db, messages, channels, said_in)
    for report, channel, card in taken:
        db.execute('UPDATE agent_reports SET body=? WHERE id=?', (breadcrumb(report, channel, TAKEN), report))
        if card:
            db.execute('UPDATE agent_report_cards SET body=? WHERE report_id=?',
                       (json.dumps(wordless(card, withdrawn=True)), report))
        record(db, 'report', report, [])  # Nothing of it is left to take.
    for report in held:
        hold(db, 'report', report)
    for channel in dropped:
        db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (channel,))
    for channel in kept_back:
        hold(db, 'summary', channel)
    if observed:
        messages = sorted({str(message) for message in messages if str(message).isdigit()})
        channels = sorted({str(channel) for channel in channels})
        db.execute(f"DELETE FROM fri_explore_notes WHERE source_id IN ({','.join('?'*len(messages))}) OR channel_id "
                   f"IN ({','.join('?'*len(channels))})", (*messages, *channels))
    return bool(taken or held or dropped or kept_back or observed)
