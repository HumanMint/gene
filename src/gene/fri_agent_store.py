"""Trusted, persistent attention controls. Model text never grants authority."""

import json
import random
import re
import time
import uuid

from .fri_memory import personal_note
from .privacy import clean

DEFAULTS = {
    'enabled': False, 'learning_enabled': True, 'proactive': True, 'support_dms': True,
    'attention_model': 'gpt-6-luna', 'attention_effort': 'low',
    'summary_model': 'gpt-6-luna', 'summary_effort': 'medium',
    'heartbeat_seconds': 180, 'learning_first_minutes': 30, 'learning_later_minutes': 360,
    'history_days': 7, 'history_batch': 30, 'history_daily_messages': 360,
    'dm_turns': 8, 'public_turns': 20, 'cooldown_min_minutes': 10,
    'cooldown_max_minutes': 20, 'voice_notes': '', 'attention_notes': '', 'learning_notes': '',
}
RANGES = {'heartbeat_seconds': (60, 1800), 'learning_first_minutes': (15, 360),
          'learning_later_minutes': (60, 1440), 'history_days': (1, 14),
          'history_batch': (5, 40), 'history_daily_messages': (30, 720),
          'dm_turns': (3, 30), 'public_turns': (5, 60),
          'cooldown_min_minutes': (10, 20), 'cooldown_max_minutes': (10, 20)}


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_reports (
            id TEXT PRIMARY KEY, at REAL NOT NULL, body TEXT NOT NULL, delivered INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS agent_summaries (
            channel_id TEXT PRIMARY KEY, body TEXT NOT NULL, authors TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_sessions (
            user_id TEXT NOT NULL, space TEXT NOT NULL, started REAL NOT NULL,
            turns INTEGER NOT NULL, until REAL NOT NULL, PRIMARY KEY(user_id,space));
        CREATE TABLE IF NOT EXISTS agent_usage_roles (attempt_id TEXT PRIMARY KEY, role TEXT NOT NULL);
    ''')


class AgentStore:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    @staticmethod
    def get(db, key, default=None):
        row = db.execute('SELECT value FROM agent_state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute('INSERT INTO agent_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(value)))

    def config(self, db):
        return {**DEFAULTS, **self.get(db, 'config', {})}

    def state(self):
        with self.store.connection() as db:
            db.execute('DELETE FROM agent_summaries WHERE at<?', (time.time()-2*86400,))
            db.execute('DELETE FROM agent_sessions WHERE started<? AND until<?',
                       (time.time()-86400, time.time()))
            db.execute('DELETE FROM agent_reports WHERE delivered=1 AND at<?', (time.time()-7*86400,))
            return {'config': self.config(db), 'version': self.get(db, 'version', 0),
                    'started': self.get(db, 'started', 0), 'next_learning': self.get(db, 'next_learning', 0),
                    'history_channels': self.settings.agent_history_channels.split(','),
                    'channels': self.settings.agent_channels.split(','),
                    'role_id': self.settings.agent_role_id,
                    'report_pending': bool(db.execute('SELECT 1 FROM agent_reports WHERE delivered=0').fetchone())}

    def update(self, changes, version, actor):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if version != self.get(db, 'version', 0):
                raise ValueError('Settings changed. Request a fresh preview.')
            config = self.config(db)
            if not changes or set(changes)-set(DEFAULTS):
                raise ValueError('Only the listed behavior settings can be changed')
            for key, value in changes.items():
                if key in RANGES:
                    low, high = RANGES[key]
                    if type(value) is not int or not low <= value <= high:
                        raise ValueError(f'{key} must be between {low} and {high}')
                elif type(DEFAULTS[key]) is bool:
                    if type(value) is not bool:
                        raise ValueError('Expected a boolean setting')
                elif key.endswith('_model'):
                    if not isinstance(value, str) or not db.execute('SELECT 1 FROM chat_models WHERE id=?', (value,)).fetchone():
                        raise ValueError('Refresh the model catalog and choose an available model')
                elif key.endswith('_effort'):
                    if value not in ('low', 'medium', 'high'):
                        raise ValueError('Use low, medium, or high effort for background work')
                elif not isinstance(value, str) or len(value) > 1500 or clean(value, 1500) != value:
                    raise ValueError('Behavior notes must be at most 1500 characters without secrets')
            config.update(changes)
            if config['cooldown_min_minutes'] > config['cooldown_max_minutes']:
                raise ValueError('Minimum cooldown cannot exceed maximum')
            self.put(db, 'config', config)
            self.put(db, 'version', version+1)
            if config['enabled'] and not self.get(db, 'started', 0):
                self.put(db, 'started', time.time())
            if {'learning_first_minutes', 'learning_later_minutes'} & set(changes):
                first = time.time()-self.get(db, 'started', time.time()) < 86400
                self.put(db, 'next_learning', time.time()+60*config['learning_first_minutes' if first else 'learning_later_minutes'])
            self.store._audit(db, actor, 'fri_behavior_changed', str(version+1), json.dumps(changes))
        return self.state()

    def admit(self, user, space):
        if space not in ('dm', 'public'):
            raise ValueError('Invalid conversation space')
        now = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            config = self.config(db)
            row = db.execute('SELECT * FROM agent_sessions WHERE user_id=? AND space=?', (user, space)).fetchone()
            if row and row['until'] > now:
                return {'allowed': False, 'notice': False, 'until': row['until']}
            if user == self.settings.owner_id or not config['enabled']:
                return {'allowed': True}
            started, turns = (row['started'], row['turns']) if row else (now, 0)
            if now-started > 1800 or (row and row['until']):
                started, turns = now, 0
            until = 0
            if turns >= config[space+'_turns']:
                until = now + random.SystemRandom().randint(config['cooldown_min_minutes']*60,
                                                             config['cooldown_max_minutes']*60)
            db.execute('INSERT INTO agent_sessions VALUES (?,?,?,?,?) ON CONFLICT(user_id,space) '
                       'DO UPDATE SET started=excluded.started,turns=excluded.turns,until=excluded.until',
                       (user, space, started, turns+1, until))
            return {'allowed': not until, 'notice': bool(until), 'until': until}

    def claim(self, manual=False):
        now = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            c = self.config(db)
            def blocked(reason):
                return {'blocked': reason} if manual else None

            if not c['enabled'] or not c['learning_enabled']:
                return blocked('Learning is paused. Changing that still needs an owner settings preview.')
            if manual and now < self.get(db, 'manual_learning_after', 0):
                return blocked('An owner-requested pass can run at most once every five minutes.')
            if not manual and now < self.get(db, 'next_learning', 0):
                return None
            # A report outage pauses learning, so the owner does not lose visibility.
            if db.execute('SELECT 1 FROM agent_reports WHERE delivered=0').fetchone():
                return blocked('The previous learning report still needs to reach the owner.')
            pending = self.get(db, 'learning_claim')
            if pending:
                if now-pending['at'] < 600:
                    return blocked('A learning pass is already running.')
                db.execute('INSERT OR IGNORE INTO agent_reports VALUES (?,?,?,0)',
                           (pending['id'], now, 'Learning pass interrupted. Reserved scan allowance retained; no automatic replay.'))
                self.put(db, 'learning_claim', None)
                return blocked('The previous pass was interrupted. Its report is pending; its allowance remains reserved.')
            day = int((now-self.get(db, 'started', now))//86400)
            budget = self.get(db, 'budget', {'day': day, 'used': 0})
            if budget['day'] != day:
                budget = {'day': day, 'used': 0}
            limit = min(c['history_batch'], c['history_daily_messages']-budget['used'])
            channels = [x for x in self.settings.agent_history_channels.split(',') if x]
            if limit <= 0 or not channels:
                return blocked('The daily scan allowance is used up or no learning channels are configured.')
            cursors = self.get(db, 'cursors', {})
            index = self.get(db, 'channel_index', 0)
            # Once caught up, a channel has no further retrospective work this week.
            for offset in range(len(channels)):
                channel = channels[(index+offset) % len(channels)]
                cursor = cursors.get(channel, {})
                if cursor.get('done_at', 0) <= now-7*86400:
                    break
            else:
                return blocked('The configured recent-history slices have already been checked. No archive replay is needed.')
            if cursor.get('done_at'):
                cursor = {}
            claim = {'id': uuid.uuid4().hex, 'at': now, 'channel_id': channel,
                     'before': cursor.get('before'), 'limit': limit,
                     'after': now-c['history_days']*86400, 'role_id': self.settings.agent_role_id,
                     'model': c['summary_model'], 'effort': c['summary_effort']}
            budget['used'] += limit
            self.put(db, 'budget', budget)
            self.put(db, 'learning_claim', claim)
            self.put(db, 'channel_index', index+offset+1)
            if manual:
                self.put(db, 'manual_learning_after', now+300)
            first = now-self.get(db, 'started', now) < 86400
            self.put(db, 'next_learning', now+60*c['learning_first_minutes' if first else 'learning_later_minutes'])
            return {**claim, 'used_today': budget['used'], 'daily_limit': c['history_daily_messages']}

    def finish(self, batch_id, records, result, scanned, before, done, counters, error='', oldest=None, newest=None):
        now = time.time()
        if not isinstance(result.get('notes', []), list) or not isinstance(result.get('summary', ''), str):
            raise ValueError('Invalid summary output')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = self.get(db, 'learning_claim')
            if not claim or claim['id'] != batch_id:
                raise ValueError('Learning claim expired or already completed')
            if not 0 <= scanned <= claim['limit'] or len(records) > scanned:
                raise ValueError('Invalid learning batch size')
            accepted = {}
            for r in records:
                if r.get('role_id') != claim['role_id'] or not claim['after'] <= r['at'] <= now:
                    continue
                revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (r['user_id'],)).fetchone()
                profile = self.store._personal_context(db, self.settings.guild_id, 'public:'+claim['channel_id'], r['user_id'])
                if profile['enabled'] and r.get('revision') == (revision[0] if revision else 0):
                    accepted[r['id']] = r
            kept = []
            for candidate in result.get('notes', [])[:5]:
                if not isinstance(candidate, dict) or not isinstance(candidate.get('source_id'), str):
                    continue
                r = accepted.get(candidate.get('source_id'))
                note = personal_note(candidate, r['text']) if r else None
                if note:
                    updated = db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at) '
                        'VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category) '
                        'DO UPDATE SET evidence=excluded.evidence,at=excluded.at WHERE excluded.at>personal_notes.at',
                        (self.settings.guild_id, 'public:'+claim['channel_id'], r['user_id'], *note, r['at']))
                    if updated.rowcount:
                        kept.append(f"{r['user_id']} / {note[0]}: {note[1]}")
            summary = clean(result.get('summary', ''), 600)
            if not accepted or len(accepted) != len(records):
                summary = ''
            # Any opt-out/revision change invalidates the entire derived summary.
            if summary and accepted and len(accepted) == len(records):
                db.execute('INSERT INTO agent_summaries VALUES (?,?,?,?) ON CONFLICT(channel_id) '
                           'DO UPDATE SET body=excluded.body,authors=excluded.authors,at=excluded.at',
                           (claim['channel_id'], summary, json.dumps(sorted({r['user_id'] for r in accepted.values()})), now))
            cursors = self.get(db, 'cursors', {})
            cursors[claim['channel_id']] = {'before': before, 'done_at': now if done else 0}
            self.put(db, 'cursors', cursors)
            self.put(db, 'learning_claim', None)
            dates = [r['at'] for r in records]
            if oldest is not None and newest is not None and claim['after'] <= oldest <= newest <= now:
                dates = [oldest, newest]
            window = ('<t:'+str(int(min(dates)))+':f> to <t:'+str(int(max(dates)))+':f>') if dates else 'no eligible messages'
            report = (f"Learning pass {batch_id[:8]}\nChannel: <#{claim['channel_id']}>; FF ADMEN only\n"
                      f"Scanned {scanned}; eligible {len(accepted)}; saved exact preferences {len(kept)}\n"
                      f"Examined range: {window}\nLookback floor: <t:{int(claim['after'])}:f>\n"
                      f"Model: {claim['model']} / {claim['effort']}\n"
                      f"Tokens: input {counters.get('input_tokens', 'unknown')}, output {counters.get('output_tokens', 'unknown')}, "
                      f"reasoning {counters.get('reasoning_output_tokens', 'unknown')}\n"
                      f"Next eligible pass: <t:{int(self.get(db, 'next_learning', now))}:R>\n"
                      f"Daily reserved scan allowance: {self.get(db, 'budget', {}).get('used', 0)}/{self.config(db)['history_daily_messages']}\n"
                      + ('\nConversation summary: '+summary if summary else '')
                      + ('\nSaved disclosures:\n'+'\n'.join(kept) if kept else '')
                      + ('\nIssue: '+clean(error, 180) if error else '')
                      + '\nAsk me to change learning frequency, pause learning, or show settings.')
            db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', (batch_id, now, report))
            return {'saved': len(kept), 'report_id': batch_id, 'scanned': scanned,
                    'eligible': len(accepted), 'error': bool(error)}

    def overview(self):
        """Owner-only evidence, not settings or authority. Bound both age and size."""
        now = time.time()
        with self.store.connection() as db:
            config = self.config(db)
            day = int((now-self.get(db, 'started', now))//86400)
            budget = self.get(db, 'budget', {})
            reports = [dict(r) for r in db.execute(
                'SELECT id,at,body,delivered FROM agent_reports WHERE at>? ORDER BY at DESC LIMIT 3',
                (now-7*86400,))]
            for report in reports:
                report['body'] = report['body'].replace('Untrusted summary:', 'Conversation summary:')
                channel = re.search(r'Channel: <#(\d+)>', report['body'])
                report['channel_id'] = channel[1] if channel else None
            summaries = [dict(r) for r in db.execute(
                'SELECT channel_id,body,at FROM agent_summaries WHERE at>? ORDER BY at DESC LIMIT 5',
                (now-2*86400,)) if r['channel_id'] in self.settings.agent_history_channels.split(',')]
            return {'reports': reports, 'summaries': summaries,
                    'learning_enabled': config['enabled'] and config['learning_enabled'],
                    'history_channels': [x for x in self.settings.agent_history_channels.split(',') if x],
                    'role_id': self.settings.agent_role_id, 'history_days': config['history_days'],
                    'next_learning': self.get(db, 'next_learning', 0),
                    'daily_reserved': budget.get('used', 0) if budget.get('day') == day else 0,
                    'daily_limit': config['history_daily_messages'], 'images_inspected': False}

    def context(self, channel, user):
        with self.store.connection() as db:
            row = db.execute('SELECT body FROM agent_summaries WHERE channel_id=? AND at>?',
                             (channel, time.time()-2*86400)).fetchone()
            profile = self.store._personal_context(db, self.settings.guild_id, 'public:'+channel, user)
            rev = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
            return {'summary': row[0] if row and profile['enabled'] else '',
                    'enabled': profile['enabled'], 'revision': rev[0] if rev else 0}

    def reports(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT id,body FROM agent_reports WHERE delivered=0 ORDER BY at LIMIT 1')]

    def delivered(self, report_id):
        with self.store.connection() as db:
            db.execute('UPDATE agent_reports SET delivered=1 WHERE id=?', (report_id,))

    def usage(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT coalesce(r.role,"conversation") AS role,u.model,u.effort,'
                'count(*) AS attempts,sum(u.input_tokens) AS input_tokens,sum(u.output_tokens) AS output_tokens,'
                'sum(u.reasoning_output_tokens) AS reasoning_tokens,sum(u.cached_input_tokens) AS cached_tokens '
                'FROM chat_usage u LEFT JOIN agent_usage_roles r ON r.attempt_id=u.attempt_id '
                'GROUP BY 1,2,3')]
