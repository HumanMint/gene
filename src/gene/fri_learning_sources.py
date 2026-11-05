"""Which Discord messages each learning report and channel summary read (HumanMint/gene#244), so a message taken back
takes their words with it.

A learning pass reports to the owner: the summary it made and each preference it saved, word for word. Its card repeats
the summary, and the channel summary it saves reaches Fri's context in that channel and the owner's learning tool. A
message the pass read, withdrawn later (edited or deleted, by ID or by its exchange's scope), leaves none of that: the
report and its card keep a wordless breadcrumb, pending or delivered, and the channel summary goes. So does everything
of a channel withdrawn as a whole (gene #243).

Each records the messages it read, by ID only, in a table of its own, so an older controller works on the same
database as before. Triggers clear a record with its report or summary, whichever controller deletes or replaces it.
One with no record (from before this, or saved by an older controller since) doesn't say what it read: a report goes
to its breadcrumb if the message was sent within the range its card says it examined (or it has no card), and a summary
goes if the message was sent before it was made.
"""
import json
import re

TABLE = ('CREATE TABLE IF NOT EXISTS fri_learning_sources (kind TEXT NOT NULL, item TEXT NOT NULL, '
         'messages TEXT NOT NULL, PRIMARY KEY(kind, item))')
TRIGGERS = {
    'fri_learning_sources_report_gone':
        "AFTER DELETE ON agent_reports BEGIN DELETE FROM fri_learning_sources WHERE kind='report' AND item=OLD.id; END",
    'fri_learning_sources_summary_gone': "AFTER DELETE ON agent_summaries BEGIN DELETE FROM fri_learning_sources "
                                         "WHERE kind='summary' AND item=OLD.channel_id; END",
    # The channel's next summary, from any controller, read what it says, not what this one read.
    'fri_learning_sources_summary_replaced': "AFTER UPDATE ON agent_summaries BEGIN DELETE FROM fri_learning_sources "
                                             "WHERE kind='summary' AND item=OLD.channel_id; END",
}
SLACK = 2  # Seconds, either way, between a message's own time and the time a pass recorded for it.
EPOCH = 1420070400  # Discord's, for a message ID's own time.
CHANNEL = re.compile(r'Channel: <#(\d+)>')
INTERRUPTED = 'Learning pass interrupted.'  # A report the controller wrote itself, with no member's words.
TAKEN = "Its findings were removed: a message it read was taken back."


def initialize(db):
    db.execute(TABLE)
    for name, body in TRIGGERS.items():
        db.execute(f'CREATE TRIGGER IF NOT EXISTS {name} {body}')


def record(db, kind, item, messages):
    """What a report (kind 'report', by its ID) or a channel's summary ('summary', by the channel) read."""
    db.execute('INSERT INTO fri_learning_sources VALUES (?,?,?) ON CONFLICT(kind, item) DO UPDATE SET '
               'messages=excluded.messages', (kind, str(item), json.dumps(sorted({str(m) for m in messages}))))


def sent(message):
    return ((int(message) >> 22)/1000)+EPOCH


def found(db, messages=(), channels=()):
    """The reports (ID, channel, card) and channel summaries (by channel) that read one of these messages, or may
    have, or came from one of these channels."""
    messages = {str(message) for message in messages if str(message).isdigit()}
    channels = {str(channel) for channel in channels}
    times = [sent(message) for message in messages]
    reports = []
    for row in db.execute("SELECT r.id,r.body,c.body AS card,s.messages AS read FROM agent_reports r LEFT JOIN "
                          "agent_report_cards c ON c.report_id=r.id LEFT JOIN fri_learning_sources s ON "
                          "s.kind='report' AND s.item=r.id").fetchall():
        card = json.loads(row['card']) if row['card'] else {}
        named = CHANNEL.search(row['body'])
        channel = str(card.get('channel_id') or (named[1] if named else ''))
        if row['read'] is not None:
            read = bool(messages & set(json.loads(row['read'])))
        elif card:  # Within the range it examined; one that examined none read no member's words.
            low, high = card.get('oldest'), card.get('newest')
            read = (type(low) in (int, float) and type(high) in (int, float)
                    and any(low-SLACK <= at <= high+SLACK for at in times))
        else:
            read = bool(times) and not row['body'].startswith(INTERRUPTED)
        if read or channel in channels:
            reports.append((row['id'], channel, card))
    summaries = [row['channel_id'] for row in db.execute(
        "SELECT a.channel_id,a.at,s.messages AS read FROM agent_summaries a LEFT JOIN fri_learning_sources s ON "
        "s.kind='summary' AND s.item=a.channel_id").fetchall()
        if row['channel_id'] in channels or (messages & set(json.loads(row['read'])) if row['read'] is not None
                                             else any(at <= row['at']+SLACK for at in times))]
    return reports, summaries


def naming(db, messages=(), channels=()):
    """Whether withdrawing these takes anything (see withdraw): a withdrawal only takes the write lock then."""
    return any(found(db, messages, channels))


def withdraw(db, messages=(), channels=()):
    """Messages, or whole channels, taken back: the learning reports that read them keep only a breadcrumb (no
    summary, disclosures or findings), as do their cards, pending or delivered, and the channel summaries that read
    them go. Returns whether anything was taken. Inside the caller's write transaction."""
    reports, summaries = found(db, messages, channels)
    for report, channel, card in reports:
        body = f'Learning pass {report[:8]}\n'+(f'Channel: <#{channel}>\n' if channel.isdigit() else '')+TAKEN
        db.execute('UPDATE agent_reports SET body=? WHERE id=?', (body, report))
        if card:
            db.execute('UPDATE agent_report_cards SET body=? WHERE report_id=?', (json.dumps(
                {**card, 'summary': '', 'findings': [], 'term': '', 'withdrawn': True}), report))
        record(db, 'report', report, [])  # Nothing of it is left to take.
    for channel in summaries:
        db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (channel,))
    return bool(reports or summaries)
