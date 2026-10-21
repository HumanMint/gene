"""Controller-owned personal reminders and explicitly published game-night plans."""

import datetime as dt
import hashlib
import json
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .fri_workspace_store import Workspace
from .privacy import redact

ACTIVE = ('pending', 'scheduled', 'claimed', 'sending')


def local_timestamp(value, timezone, *, now=None):
    """Reject nonexistent/ambiguous wall times rather than guessing a DST fold."""
    now = time.time() if now is None else now
    try:
        zone = ZoneInfo(timezone)
        wall = dt.datetime.fromisoformat(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError('Use a local date/time and an IANA timezone, such as America/Los_Angeles or UTC.') from None
    if wall.tzinfo is not None or wall.second or wall.microsecond:
        raise ValueError('Use a local date/time to the minute, with the timezone supplied separately.')
    candidates = {wall.replace(tzinfo=zone, fold=fold).timestamp() for fold in (0, 1)
                  if dt.datetime.fromtimestamp(wall.replace(tzinfo=zone, fold=fold).timestamp(), zone).replace(tzinfo=None) == wall}
    if len(candidates) != 1:
        raise ValueError('That local time is ambiguous or does not exist because of daylight saving time. Choose an unambiguous time, or supply the exact time in UTC.')
    timestamp = candidates.pop()
    if not now+60 <= timestamp <= now+30*86400:
        raise ValueError('Choose a time at least one minute and at most 30 days from now.')
    return timestamp


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fri_reminders (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            dm_id TEXT NOT NULL, body TEXT NOT NULL, local_time TEXT NOT NULL,
            timezone TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL,
            revision INTEGER NOT NULL, source_id TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL, preview_id TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL, expires REAL NOT NULL, lease TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0, message_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS fri_plans (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, title TEXT NOT NULL, slots TEXT NOT NULL,
            timezone TEXT NOT NULL, state TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT -1,
            revision INTEGER NOT NULL, source_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
            preview_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fri_plan_votes (
            plan_id TEXT NOT NULL REFERENCES fri_plans(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL, available TEXT NOT NULL DEFAULT '[]', rsvp TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(plan_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS fri_social_events (
            event_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, user_id TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL
        );
    ''')


def erase(db, user):
    db.execute('DELETE FROM fri_social_events WHERE user_id=? OR item_id IN '
               '(SELECT id FROM fri_reminders WHERE user_id=? UNION SELECT id FROM fri_plans WHERE user_id=?)', (user, user, user))
    db.execute('DELETE FROM fri_reminders WHERE user_id=?', (user,))
    db.execute('DELETE FROM fri_plans WHERE user_id=?', (user,))
    db.execute('DELETE FROM fri_plan_votes WHERE user_id=?', (user,))


def fresh(source, scope, *, interaction=False):
    source.check(scope['guild_id'], scope['conversation_id'], scope['user_id'])
    if source.kind != ('discord_interaction' if interaction else 'discord_message') or not -5 <= time.time()-source.timestamp().timestamp() <= (120 if interaction else 300):
        raise ValueError('A matching fresh Discord event is required.')


def digest(*items):
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


class Social:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def prune(db):
        now = time.time()
        db.execute("UPDATE fri_reminders SET state='expired' WHERE state='pending' AND created<?", (now-900,))
        # A crash before the sending fence cannot post; after it, never replay.
        db.execute("UPDATE fri_reminders SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'failed' END WHERE state IN ('claimed','sending') AND lease_until<?", (now,))
        db.execute('DELETE FROM fri_reminders WHERE expires<?', (now,))
        db.execute('DELETE FROM fri_plans WHERE expires<?', (now,))
        db.execute("UPDATE fri_plans SET state='expired' WHERE state='pending' AND created<?", (now-900,))
        db.execute('DELETE FROM fri_social_events WHERE created<?', (now-31*86400,))

    @staticmethod
    def reminder_row(db, item, scope=None):
        row = db.execute('SELECT * FROM fri_reminders WHERE id=?', (item,)).fetchone()
        if not row or (scope and (row['guild_id'] != scope['guild_id'] or row['user_id'] != scope['user_id'] or scope['conversation_id'] != 'private:'+row['dm_id'])):
            raise KeyError('Reminder unavailable in this DM')
        return dict(row)

    def reminder_prepare(self, scope, source, revision, body, local_time, timezone):
        fresh(source, scope)
        if source.guild_id is not None or not scope['conversation_id'].startswith('private:'):
            raise ValueError('Prepare personal reminders in your verified member DM.')
        if not body.strip() or len(body) > 1000 or redact(body) != body:
            raise ValueError('Keep reminder text within 1000 characters without credentials.')
        due = local_timestamp(local_time, timezone)
        payload = digest(scope, revision, body, local_time, timezone)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            Workspace.revision(db, scope['user_id'], revision)
            old = db.execute('SELECT * FROM fri_reminders WHERE source_id=?', (source.id,)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('This message already prepared another reminder. Send a new message to change it.')
                return dict(old)
            if db.execute("SELECT COUNT(*) FROM fri_reminders WHERE user_id=? AND state IN ('pending','scheduled','claimed','sending')", (scope['user_id'],)).fetchone()[0] >= 5:
                raise ValueError('You can have up to five pending or scheduled reminders. Cancel one first.')
            if db.execute("SELECT COUNT(*) FROM fri_reminders WHERE state IN ('pending','scheduled','claimed','sending')").fetchone()[0] >= 200:
                raise ValueError('The server reminder allowance is full.')
            item, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO fri_reminders(id,guild_id,user_id,dm_id,body,local_time,timezone,due,state,revision,source_id,payload,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (item, scope['guild_id'], scope['user_id'], source.channel_id, body, local_time, timezone, due, 'pending', revision, source.id, payload, now, due+7*86400))
            return self.reminder_row(db, item)

    def reminder_list(self, scope):
        with self.store.connection() as db:
            self.prune(db)
            return {'items': [dict(r) for r in db.execute('SELECT id,body,due,local_time,timezone,state,message_id FROM fri_reminders WHERE guild_id=? AND user_id=? AND dm_id=? ORDER BY due DESC LIMIT 15',
                            (scope['guild_id'], scope['user_id'], scope['conversation_id'].split(':')[1]))]}

    def bind(self, kind, item, scope, message_id):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.reminder_row(db, item, scope) if kind == 'reminder' else self.plan_row(db, item, scope)
            if row['user_id'] != scope['user_id']:
                raise ValueError('Only the organizer can bind a preview.')
            if row['state'] != 'pending' or (row['preview_id'] and row['preview_id'] != message_id):
                raise ValueError('This preview is already bound or inactive.')
            table = 'fri_reminders' if kind == 'reminder' else 'fri_plans'
            db.execute(f'UPDATE {table} SET preview_id=? WHERE id=?', (message_id, item))

    def reminder_decide(self, item, scope, source, revision, decision, preview_id=''):
        fresh(source, scope, interaction=bool(preview_id))
        if source.guild_id is not None or decision not in ('confirm', 'cancel'):
            raise ValueError('Use your own reminder DM preview.')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            row = self.reminder_row(db, item, scope)
            if preview_id and (not row['preview_id'] or preview_id != row['preview_id']):
                raise ValueError('Use the original bound reminder preview.')
            if decision == 'confirm':
                Workspace.revision(db, scope['user_id'], revision)
                if not preview_id or row['revision'] != revision or row['due'] < time.time()+30:
                    raise ValueError('Prepare a fresh reminder preview; its time or privacy settings changed.')
                if row['state'] == 'pending':
                    db.execute("UPDATE fri_reminders SET state='scheduled' WHERE id=?", (item,))
            elif row['state'] in ('pending', 'scheduled', 'claimed'):
                db.execute("UPDATE fri_reminders SET state='cancelled',body='' WHERE id=?", (item,))
            return self.reminder_row(db, item, scope)

    def reminder_claim(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            for row in db.execute("SELECT * FROM fri_reminders WHERE state='scheduled' AND due<=? ORDER BY due LIMIT 10", (time.time(),)).fetchall():
                try:
                    Workspace.revision(db, row['user_id'], row['revision'])
                except ValueError:
                    db.execute("UPDATE fri_reminders SET state='cancelled',body='' WHERE id=?", (row['id'],))
                    continue
                lease = uuid.uuid4().hex
                db.execute("UPDATE fri_reminders SET state='claimed',lease=?,lease_until=? WHERE id=?", (lease, time.time()+120, row['id']))
                return {**dict(row), 'state': 'claimed', 'lease': lease}
            return None

    def reminder_sending(self, item, lease):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            row = self.reminder_row(db, item)
            if row['state'] != 'claimed' or row['lease'] != lease:
                raise ValueError('Reminder delivery is no longer authorized.')
            Workspace.revision(db, row['user_id'], row['revision'])
            db.execute("UPDATE fri_reminders SET state='sending',lease_until=? WHERE id=?", (time.time()+120, item))
            return {**row, 'state': 'sending'}

    def reminder_finish(self, item, lease, state, message_id=''):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.reminder_row(db, item)
            if row['lease'] != lease or row['state'] not in ('claimed', 'sending', 'uncertain', state):
                raise ValueError('This reminder delivery is closed.')
            if state not in ('sent', 'failed', 'uncertain') or (state == 'sent' and (not message_id or (row['message_id'] and row['message_id'] != message_id))):
                raise ValueError('A successful delivery needs its exact Discord message ID.')
            db.execute('UPDATE fri_reminders SET state=?,message_id=? WHERE id=?', (state, message_id, item))
            return {'state': state}

    @staticmethod
    def plan_row(db, item, scope=None):
        row = db.execute('SELECT * FROM fri_plans WHERE id=?', (item,)).fetchone()
        if not row or (scope and (row['guild_id'] != scope['guild_id'] or scope['conversation_id'] != 'public:'+row['channel_id'])):
            raise KeyError('Plan unavailable in this channel')
        result = dict(row)
        result['slots'] = json.loads(result['slots'])
        votes = db.execute('SELECT available,rsvp FROM fri_plan_votes WHERE plan_id=?', (item,)).fetchall()
        result['available'] = [sum(i in json.loads(v['available']) for v in votes) for i in range(len(result['slots']))]
        result['rsvps'] = {choice: sum(v['rsvp'] == choice for v in votes) for choice in ('going', 'maybe', 'cannot')}
        return result

    def plan_prepare(self, scope, source, revision, title, local_times, timezone, request_text):
        fresh(source, scope)
        if source.guild_id is None or not scope['conversation_id'].startswith('public:'):
            raise ValueError('Prepare game-night cards in an allowed server channel.')
        values = local_times.splitlines()
        slots = [local_timestamp(value.strip(), timezone) for value in values]
        if not 2 <= len(slots) <= 4 or len(set(slots)) != len(slots):
            raise ValueError('Choose two to four distinct future time options.')
        if not title.strip() or len(title) > 100 or redact(title) != title:
            raise ValueError('Keep the game-night title within 100 characters without credentials.')
        if title.casefold() not in request_text.casefold():
            raise ValueError('Use an activity title from the organizer’s current message. Ask them to supply it rather than saving other members’ context.')
        payload = digest(scope, revision, title, local_times, timezone, request_text)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            Workspace.revision(db, scope['user_id'], revision)
            old = db.execute('SELECT id,payload FROM fri_plans WHERE source_id=?', (source.id,)).fetchone()
            if old:
                if old['payload'] != payload:
                    raise ValueError('This message already prepared another plan. Send a fresh message to change it.')
                return self.plan_row(db, old['id'], scope)
            if db.execute("SELECT COUNT(*) FROM fri_plans WHERE user_id=? AND state IN ('pending','open','finalized')", (scope['user_id'],)).fetchone()[0] >= 2 or db.execute("SELECT COUNT(*) FROM fri_plans WHERE state IN ('pending','open','finalized')").fetchone()[0] >= 10:
                raise ValueError('Limit: two active plans per organizer and ten per server. Cancel an old plan first.')
            item, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO fri_plans(id,guild_id,user_id,channel_id,title,slots,timezone,state,revision,source_id,payload,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (item, scope['guild_id'], scope['user_id'], source.channel_id, title, json.dumps(slots), timezone, 'pending', revision, source.id, payload, now, max(slots)+86400))
            return self.plan_row(db, item, scope)

    def plan_decide(self, item, scope, source, revision, decision, preview_id):
        fresh(source, scope, interaction=True)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            Workspace.revision(db, scope['user_id'], revision)
            row = self.plan_row(db, item, scope)
            if not row['preview_id'] or row['preview_id'] != preview_id:
                raise ValueError('Use the original game-night card.')
            payload = digest(item, scope['user_id'], decision, preview_id)
            old = db.execute('SELECT payload FROM fri_social_events WHERE event_id=?', (source.id,)).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError('This interaction already performed a different action.')
                return row
            organizer = row['user_id'] == scope['user_id']
            if decision == 'publish' and organizer and row['state'] == 'pending':
                db.execute("UPDATE fri_plans SET state='open' WHERE id=?", (item,))
            elif decision == 'cancel' and organizer and row['state'] in ('pending', 'open', 'finalized'):
                db.execute("UPDATE fri_plans SET state='cancelled' WHERE id=?", (item,))
            elif decision.startswith('final') and organizer and row['state'] == 'open' and decision[5:].isdigit() and int(decision[5:]) < len(row['slots']):
                chosen = int(decision[5:])
                if row['slots'][chosen] <= time.time():
                    raise ValueError('That time has passed. Choose a future option.')
                db.execute("UPDATE fri_plans SET state='finalized',selected=? WHERE id=?", (chosen, item))
            elif ((decision.startswith('vote') and row['state'] == 'open' and decision[4:].isdigit() and int(decision[4:]) < len(row['slots'])) or (decision in ('going', 'maybe', 'cannot') and row['state'] == 'finalized')):
                vote = db.execute('SELECT * FROM fri_plan_votes WHERE plan_id=? AND user_id=?', (item, scope['user_id'])).fetchone()
                if not vote and db.execute('SELECT COUNT(*) FROM fri_plan_votes WHERE plan_id=?', (item,)).fetchone()[0] >= 100:
                    raise ValueError('This plan has reached its participant limit.')
                available, rsvp = (json.loads(vote['available']), vote['rsvp']) if vote else ([], '')
                if decision.startswith('vote'):
                    chosen = int(decision[4:])
                    if row['slots'][chosen] <= time.time():
                        raise ValueError('That time has passed.')
                    available = [i for i in available if i != chosen] if chosen in available else available+[chosen]
                else:
                    rsvp = decision
                db.execute('INSERT INTO fri_plan_votes VALUES (?,?,?,?) ON CONFLICT(plan_id,user_id) DO UPDATE SET available=excluded.available,rsvp=excluded.rsvp', (item, scope['user_id'], json.dumps(sorted(available)), rsvp))
            else:
                raise ValueError('That action is unavailable; only the organizer can publish, finalize or cancel.')
            db.execute('INSERT INTO fri_social_events VALUES (?,?,?,?,?)', (source.id, item, scope['user_id'], payload, time.time()))
            return self.plan_row(db, item, scope)
