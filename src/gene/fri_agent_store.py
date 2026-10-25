"""Trusted, persistent attention controls. Model text never grants authority."""

import json
import math
import random
import re
import time
import uuid

from .fri_explore_store import Exploration
from .fri_explore_store import initialize as initialize_exploration
from .fri_explore_store import notes as historical_notes
from .fri_memory import personal_note
from .fri_memory_store import last_removal
from .privacy import clean

DEFAULTS = {
    'enabled': False, 'learning_enabled': True, 'proactive': True, 'support_dms': True,
    'attention_model': 'gpt-6-luna', 'attention_effort': 'low',
    'summary_model': 'gpt-6-luna', 'summary_effort': 'medium',
    'heartbeat_seconds': 180, 'learning_first_minutes': 30, 'learning_later_minutes': 360,
    'history_days': 7, 'history_batch': 30, 'history_daily_messages': 360,
    'exploration_enabled': False, 'exploration_days': 365,
    'meme_enabled': False, 'meme_model': 'gpt-6-luna', 'meme_effort': 'medium',
    'meme_interval_minutes': 30, 'meme_daily_checks': 24, 'meme_daily_reactions': 4,
    'status_enabled': False, 'status_model': 'gpt-6-luna', 'status_effort': 'low',
    'status_rotation_minutes': 360,
    'dm_turns': 8, 'public_turns': 20, 'cooldown_min_minutes': 10,
    'cooldown_max_minutes': 20, 'voice_notes': '', 'attention_notes': '', 'learning_notes': '',
    'reply_check_enabled': False, 'reply_check_private': False, 'reply_check_model': 'gpt-6-luna',
    'reply_check_effort': 'low', 'reply_check_minutes': 30, 'reply_check_daily_pairs': 120,
}
RANGES = {'heartbeat_seconds': (60, 1800), 'learning_first_minutes': (15, 360),
          'learning_later_minutes': (60, 1440), 'history_days': (1, 14),
          'history_batch': (5, 40), 'history_daily_messages': (30, 720),
          'exploration_days': (30, 5000),
          'meme_interval_minutes': (15, 360), 'meme_daily_checks': (2, 48),
          'meme_daily_reactions': (0, 8),
          'status_rotation_minutes': (60, 720),
          'dm_turns': (3, 30), 'public_turns': (5, 60),
          'cooldown_min_minutes': (10, 20), 'cooldown_max_minutes': (10, 20),
          'reply_check_minutes': (10, 720), 'reply_check_daily_pairs': (10, 400)}


def snowflake_floor(timestamp):
    """First possible Discord ID in this millisecond (an exclusive upper bound)."""
    return str(max(0, int(timestamp*1000)-1420070400000) << 22)


def initialize(db):
    initialize_exploration(db)
    from .fri_operation_store import initialize as initialize_operations
    initialize_operations(db)
    from .fri_reply_checks import initialize as initialize_reply_checks
    initialize_reply_checks(db)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_reports (
            id TEXT PRIMARY KEY, at REAL NOT NULL, body TEXT NOT NULL, delivered INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS agent_report_cards (
            report_id TEXT PRIMARY KEY REFERENCES agent_reports(id) ON DELETE CASCADE,
            body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_summaries (
            channel_id TEXT PRIMARY KEY, body TEXT NOT NULL, authors TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS agent_sessions (
            user_id TEXT NOT NULL, space TEXT NOT NULL, started REAL NOT NULL,
            turns INTEGER NOT NULL, until REAL NOT NULL, PRIMARY KEY(user_id,space));
        CREATE TABLE IF NOT EXISTS agent_usage_roles (attempt_id TEXT PRIMARY KEY, role TEXT NOT NULL);
    ''')
    if 'revisions' not in {row[1] for row in db.execute('PRAGMA table_info(agent_summaries)')}:
        # Authors' privacy revisions observed when the summary was made; older rows fail closed.
        db.execute("ALTER TABLE agent_summaries ADD COLUMN revisions TEXT NOT NULL DEFAULT '{}'")


class AgentStore:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.exploration = Exploration(self)

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

    def learning_schedule(self, db, now):
        config = self.config(db)
        window = self.get(db, 'learning_window')
        if window and window['until'] > now:
            return {'minutes': window['minutes'], 'window': window}
        first = now-self.get(db, 'started', now) < 86400
        return {'minutes': config['learning_first_minutes' if first else 'learning_later_minutes'], 'window': None}

    def start_learning_window(self, minutes, hours, version, actor):
        """Trusted maintainer operation; no Discord endpoint or model action grants this authority."""
        if type(minutes) is not int or not 15 <= minutes <= 360 or type(hours) is not int or not 1 <= hours <= 24:
            raise ValueError('Use a 15–360 minute interval for 1–24 hours')
        now = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if version != self.get(db, 'version', 0):
                raise ValueError('Settings changed. Request a fresh preview.')
            config = self.config(db)
            if not config['enabled'] or not config['learning_enabled']:
                raise ValueError('Enable learning before starting a temporary schedule')
            window = {'minutes': minutes, 'started': now, 'until': now+hours*3600}
            self.put(db, 'learning_window', window)
            self.put(db, 'version', version+1)
            self.put(db, 'next_learning', now)
            self.store._audit(db, actor, 'fri_learning_window', str(version+1), json.dumps(window))
        return self.state()

    def state(self):
        with self.store.connection() as db:
            db.execute('DELETE FROM agent_summaries WHERE at<?', (time.time()-2*86400,))
            db.execute('DELETE FROM agent_sessions WHERE started<? AND until<?',
                       (time.time()-86400, time.time()))
            db.execute('DELETE FROM agent_reports WHERE delivered=1 AND at<?', (time.time()-7*86400,))
            return {'config': self.config(db), 'version': self.get(db, 'version', 0),
                    'auto_repairs_enabled': self.store._setting(db, 'repair_policy') == 'existing-features-v1',
                    'learning_schedule': self.learning_schedule(db, time.time()),
                    'started': self.get(db, 'started', 0), 'next_learning': self.get(db, 'next_learning', 0),
                    'history_channels': self.settings.agent_history_channels.split(','),
                    'channels': self.settings.agent_channels.split(','),
                    'meme_channels': [c for c in self.settings.meme_channel_ids.split(',') if c],
                    'status_context_channels': [c for c in self.settings.status_context_channels.split(',') if c],
                    'test_scope': {'guild_id': self.settings.test_guild_id,
                                   'channel_id': self.settings.test_channel_id},
                    'web_search_enabled': self.settings.web_search_enabled,
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
            was_checking = config['enabled'] and config['reply_check_enabled']
            config.update(changes)
            if config['cooldown_min_minutes'] > config['cooldown_max_minutes']:
                raise ValueError('Minimum cooldown cannot exceed maximum')
            self.put(db, 'config', config)
            self.put(db, 'version', version+1)
            if config['enabled'] and not self.get(db, 'started', 0):
                self.put(db, 'started', time.time())
            if config['enabled'] and config['reply_check_enabled'] and not was_checking:
                from .fri_reply_checks import start as start_reply_checks
                start_reply_checks(db, self.put)
            if {'learning_first_minutes', 'learning_later_minutes'} & set(changes):
                # A later explicit owner schedule change supersedes the temporary cadence.
                self.put(db, 'learning_window', None)
                self.put(db, 'next_learning', time.time()+60*self.learning_schedule(db, time.time())['minutes'])
            self.store._audit(db, actor, 'fri_behavior_changed', str(version+1), json.dumps(changes))
        return self.state()

    def admit(self, user, space, guild=None):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self.admit_in(db, user, space, guild)

    def admit_in(self, db, user, space, guild=None):
        """Reserve the same member allowance within an event admission transaction."""
        if space not in ('dm', 'public'):
            raise ValueError('Invalid conversation space')
        limit_key = space+'_turns'
        if guild and guild != self.settings.guild_id:
            space += ':'+guild
        now = time.time()
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
        if turns >= config[limit_key]:
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
            remaining = c['history_daily_messages']-budget['used']
            limit = min(c['history_batch'], remaining)
            channels = [x for x in self.settings.agent_history_channels.split(',') if x]
            if limit <= 0 or not channels:
                return blocked('The daily scan allowance is used up or no learning channels are configured.')
            cursors = self.get(db, 'cursors', {})
            index = self.get(db, 'channel_index', 0)
            channel = channels[index % len(channels)]
            cursor = dict(cursors.get(channel, {}))
            schedule = self.learning_schedule(db, now)
            if schedule['window'] and not manual:
                reset = self.get(db, 'started', now)+(day+1)*86400
                slots = max(1, math.ceil((reset-now)/(schedule['minutes']*60)))
                limit = min(limit, max(1, math.ceil(remaining/slots)))
            floor = now-c['history_days']*86400
            until_id = snowflake_floor(now)
            if 'after_id' not in cursor:
                # Legacy backward cursors never stored the newest scanned ID. Resume
                # from enablement, not their oldest message or their later completion.
                boundary = max(floor, self.get(db, 'started', now)) if cursor else now
                cursor['after_id'] = str(int(snowflake_floor(boundary))-1)
            mode = 'recent' if cursor.get('done_at') else 'history'
            claim = {'id': uuid.uuid4().hex, 'at': now, 'channel_id': channel,
                     'mode': mode, 'cursor': cursor, 'budget_day': day,
                     'before': cursor.get('before') if mode == 'history' else None,
                     'after_id': max(cursor['after_id'], str(int(snowflake_floor(floor))-1), key=int) if mode == 'recent' else None,
                     'until_id': until_id, 'limit': limit,
                     'after': floor, 'role_id': self.settings.agent_role_id,
                     'model': c['summary_model'], 'effort': c['summary_effort']}
            if c['exploration_enabled']:
                limit = min(limit, 25)  # Discord search returns at most 25 matches.
                claim.update(mode='explore', before=None, after=now-c['exploration_days']*86400,
                             depth=c['exploration_days'], limit=limit,
                             exploration=self.exploration.context(db, channel))
            budget['used'] += limit
            self.put(db, 'budget', budget)
            self.put(db, 'learning_claim', claim)
            self.put(db, 'channel_index', index+1)
            if manual:
                self.put(db, 'manual_learning_after', now+300)
            next_learning = now+60*schedule['minutes']
            if schedule['window']:
                next_learning = min(next_learning, schedule['window']['until'])
            self.put(db, 'next_learning', next_learning)
            return {**claim, 'used_today': budget['used'], 'daily_limit': c['history_daily_messages']}

    def finish(self, batch_id, records, result, scanned, before, done, counters, error='', oldest=None, newest=None, newest_id=None,
               scanned_ids=()):
        now = time.time()
        if not isinstance(result.get('notes', []), list) or not isinstance(result.get('summary', ''), str):
            raise ValueError('Invalid summary output')
        if not isinstance(result.get('observations', []), list):
            raise ValueError('Invalid historical observations')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = self.get(db, 'learning_claim')
            if not claim or claim['id'] != batch_id:
                raise ValueError('Learning claim expired or already completed')
            if not 0 <= scanned <= claim['limit'] or len(records) > scanned:
                raise ValueError('Invalid learning batch size')
            # Only a short successful fetch establishes that this slice is drained.
            done = scanned < claim['limit']
            mode = claim.get('mode', 'history')
            if claim.get('privacy_invalidated'):
                records, result, error = [], {}, 'Privacy changed during exploration; findings discarded.'
            if mode == 'explore' and not error:
                self.exploration.validate(claim, scanned, scanned_ids)
            if mode == 'recent' and not error and scanned:
                if (not newest_id or not before or not newest_id.isdigit() or not before.isdigit() or
                        not int(claim['after_id']) < int(before) <= int(newest_id) < int(claim['until_id'])):
                    raise ValueError('Invalid new-message cursor')
            if error:
                result = {}  # Failed inference cannot produce derived memory or advance a cursor.
            accepted = {}
            from .fri_memory_store import removed
            for r in records:
                if (r.get('role_id') != claim['role_id'] or not claim['after'] <= r['at'] <= now or
                        (mode == 'explore' and (r['id'] not in scanned_ids or
                         (claim['plan']['member_id'] and r['user_id'] != claim['plan']['member_id']))) or
                        (mode == 'recent' and not int(claim['after_id']) < int(r['id']) < int(claim['until_id']))):
                    continue
                revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (r['user_id'],)).fetchone()
                profile = self.store._personal_context(db, self.settings.guild_id, 'public:'+claim['channel_id'], r['user_id'])
                # Words from before a removal teach nothing again: no note, observation or summary.
                cutoff = removed(db, self.settings.guild_id, 'public:'+claim['channel_id'], r['user_id'])
                if (profile['enabled'] and r.get('revision') == (revision[0] if revision else 0)
                        and (cutoff is None or r['at'] > cutoff)):
                    accepted[r['id']] = r
            kept = []
            for candidate in (result.get('notes', [])[:5] if mode != 'explore' else []):
                if not isinstance(candidate, dict) or not isinstance(candidate.get('source_id'), str):
                    continue
                r = accepted.get(candidate.get('source_id'))
                note = personal_note(candidate, r['text']) if r else None
                if note:
                    from .fri_memory_store import save
                    if save(db, self.settings.guild_id, 'public:'+claim['channel_id'], r['user_id'], note, r['at']):
                        kept.append(f"{r['user_id']} / {note[0]}: {note[1]}")
            summary = clean(result.get('summary', ''), 600)
            if not accepted or len(accepted) != len(records):
                summary = ''
            # Any opt-out/revision change invalidates the entire derived summary.
            if summary and accepted and len(accepted) == len(records) and mode != 'explore':
                db.execute('INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES (?,?,?,?,?) '
                           'ON CONFLICT(channel_id) DO UPDATE SET body=excluded.body,authors=excluded.authors,'
                           'at=excluded.at,revisions=excluded.revisions',
                           (claim['channel_id'], summary, json.dumps(sorted({r['user_id'] for r in accepted.values()})), now,
                            json.dumps({r['user_id']: r['revision'] for r in accepted.values()}, sort_keys=True)))
            cursors = self.get(db, 'cursors', {})
            discoveries = []
            already_checked = 0
            if not error:
                cursor = dict(claim.get('cursor', cursors.get(claim['channel_id'], {})))
                if mode == 'explore':
                    already_checked = sum(bool(db.execute('SELECT 1 FROM fri_explore_seen WHERE message_id=?', (mid,)).fetchone())
                                          for mid in scanned_ids)
                    discoveries = self.exploration.finish(db, claim, accepted, result, scanned_ids)
                elif mode == 'recent':
                    after_id = str(int(claim['until_id'])-1) if done else (newest_id or claim['after_id'])
                    cursor['after_id'] = max(cursor['after_id'], after_id, key=int)
                else:
                    cursor.update(before=before, done_at=now if done else 0)
                if mode != 'explore':
                    cursors[claim['channel_id']] = cursor
                    self.put(db, 'cursors', cursors)
                budget = self.get(db, 'budget', {})
                if claim.get('budget_day') == budget.get('day'):
                    budget['used'] = max(0, budget['used']-(claim['limit']-scanned))
                    self.put(db, 'budget', budget)
            self.put(db, 'learning_claim', None)
            dates = [r['at'] for r in records]
            if oldest is not None and newest is not None and claim['after'] <= oldest <= newest <= now:
                dates = [oldest, newest]
            window = ('<t:'+str(int(min(dates)))+':f> to <t:'+str(int(max(dates)))+':f>') if dates else 'no eligible messages'
            plan = claim.get('plan', {})
            name = ''
            exploration_report = ''
            if mode == 'explore':
                name = next((m['name'] or m['user_id'] for m in claim.get('exploration', {}).get('members', [])
                             if m['user_id'] == plan.get('member_id')), plan.get('member_id') or 'member discovery')
                name = plan.get('member_name') or name
                exploration_report = (f"Focus: {name}; term: {plan.get('term') or '(open conversation)'}\n"
                    f"Purpose: {plan.get('reason') or 'No completed search plan'}\n"
                    f"Previously checked matches: {already_checked}; new historical notes: {len(discoveries)}\n"
                    + ('\n'.join(discoveries)+'\n' if discoveries else ''))
                target = claim.get('target')
                if target:
                    exploration_report += (f"Member rotation: round {target['round']}, turn {target['position']}/{target['total']}\n"
                        + ('No unread history remains in the approved channels.\n' if target['exhausted'] else '')
                        + ('No eligible messages found in this slice; this is a checked turn, not a learned preference.\n'
                           if not accepted and not error else ''))
            report = (f"Learning pass {batch_id[:8]}\nChannel: <#{claim['channel_id']}>; FF ADMEN only\n"
                      f"Mode: {'member/topic archive search' if mode == 'explore' else 'new messages' if mode == 'recent' else 'initial history scan'}\n"
                      + exploration_report +
                      f"Scanned {scanned}; eligible {len(accepted)}; saved exact preferences {len(kept)}\n"
                      f"Examined range: {window}\nLookback floor: <t:{int(claim['after'])}:f>\n"
                      f"Model: {claim['model']} / {claim['effort']}\n"
                      f"Tokens: input {counters.get('input_tokens', 'unknown')}, output {counters.get('output_tokens', 'unknown')}, "
                      f"reasoning {counters.get('reasoning_output_tokens', 'unknown')}\n"
                      f"Next eligible pass: <t:{int(self.get(db, 'next_learning', now))}:R>\n"
                      + (f"Temporary cadence ends: <t:{int(self.learning_schedule(db, now)['window']['until'])}:f>\n"
                         if self.learning_schedule(db, now)['window'] else '')
                      + f"Daily reserved scan allowance: {self.get(db, 'budget', {}).get('used', 0)}/{self.config(db)['history_daily_messages']}\n"
                      + ('\nConversation summary: '+summary if summary else '')
                      + ('\nSaved disclosures:\n'+'\n'.join(kept) if kept else '')
                      + ('\nIssue: '+clean(error, 180) if error else '')
                      + '\nAsk me to change learning frequency, pause learning, or show settings.')
            db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', (batch_id, now, report))
            card = {'channel_id': claim['channel_id'], 'mode': mode, 'member': name,
                    'term': plan.get('term', ''), 'scanned': scanned, 'eligible': len(accepted),
                    'already_checked': already_checked, 'historical_notes': len(discoveries),
                    'saved_preferences': len(kept), 'summary': summary, 'error': bool(error),
                    'findings': discoveries[:3],
                    'rotation': {key: value for key, value in claim.get('target', {}).items()
                                 if key in {'round', 'position', 'total', 'exhausted'}},
                    'oldest': min(dates) if dates else None, 'newest': max(dates) if dates else None,
                    'next_check': self.get(db, 'next_learning', now)}
            # Presentation metadata shares the report's retention and privacy deletion boundary.
            db.execute('INSERT INTO agent_report_cards VALUES (?,?)', (batch_id, json.dumps(card)))
            return {'saved': len(kept), 'report_id': batch_id, 'scanned': scanned,
                    'eligible': len(accepted), 'historical_notes': len(discoveries), 'error': bool(error)}

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
            summaries = [{k: r[k] for k in ('channel_id', 'body', 'at')} for r in db.execute(
                'SELECT channel_id,body,at,authors,revisions FROM agent_summaries WHERE at>? ORDER BY at DESC LIMIT 5',
                (now-2*86400,)) if r['channel_id'] in self.settings.agent_history_channels.split(',')
                and self._summary_current(db, r)]  # The same revision fence as ordinary context.
            return {'reports': reports, 'summaries': summaries,
                    'historical_notes': [dict(channel_id=c, **n) for c in self.settings.agent_history_channels.split(',')
                                         for n in historical_notes(db, c)][:20],
                    'learning_schedule': self.learning_schedule(db, now),
                    'learning_enabled': config['enabled'] and config['learning_enabled'],
                    'history_channels': [x for x in self.settings.agent_history_channels.split(',') if x],
                    'role_id': self.settings.agent_role_id, 'history_days': config['history_days'],
                    'next_learning': self.get(db, 'next_learning', 0),
                    'daily_reserved': budget.get('used', 0) if budget.get('day') == day else 0,
                    'daily_limit': config['history_daily_messages'], 'images_inspected': False,
                    'scope': 'Text history learning only. Occasional meme checks are a separate capability.'}

    def context(self, channel, user, guild=None):
        guild = guild or self.settings.guild_id
        with self.store.connection() as db:
            row = db.execute('SELECT body,authors,revisions FROM agent_summaries WHERE channel_id=? AND at>?',
                             (channel, time.time()-2*86400)).fetchone()
            if row and not self._summary_current(db, row):
                db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (channel,))
                row = None
            profile = self.store._personal_context(db, guild, 'public:'+channel, user)
            rev = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
            summary = row[0] if row and profile['enabled'] and guild == self.settings.guild_id else ''
            revisions = json.loads(row['revisions']) if summary else {}
            return {'summary': summary,
                    # Replies built from the summary inherit its authors at the revisions it was made from.
                    'summary_provenance': [{'user_id': author, 'revision': revision}
                                           for author, revision in sorted(revisions.items()) if author != str(user)],
                    'historical_topics': historical_notes(db, channel, user) if profile['enabled'] and guild == self.settings.guild_id else [],
                    'enabled': profile['enabled'], 'revision': rev[0] if rev else 0,
                    'removed_at': last_removal(db, guild, user)}

    def _summary_current(self, db, row):
        """A summary is shown only while every author still has the revision it was made from."""
        revisions = json.loads(row['revisions'] or '{}')
        if not revisions or set(revisions) != set(json.loads(row['authors'])):
            return False
        return all(self.store._privacy_state(db, author) == (revision, True) for author, revision in revisions.items())

    def reports(self):
        with self.store.connection() as db:
            return [self.report_row(r) for r in db.execute(
                'SELECT r.id,r.body,c.body AS card FROM agent_reports r '
                'LEFT JOIN agent_report_cards c ON c.report_id=r.id WHERE delivered=0 ORDER BY at LIMIT 1')]

    @staticmethod
    def report_row(row):
        return {'id': row['id'], 'body': row['body'], 'card': json.loads(row['card']) if row['card'] else {}}

    def report(self, report_id):
        with self.store.connection() as db:
            row = db.execute('SELECT r.id,r.body,c.body AS card FROM agent_reports r '
                             'LEFT JOIN agent_report_cards c ON c.report_id=r.id WHERE r.id=? AND r.at>?',
                             (report_id, time.time()-7*86400)).fetchone()
            return self.report_row(row) if row else None

    def delivered(self, report_id):
        with self.store.connection() as db:
            db.execute('UPDATE agent_reports SET delivered=1 WHERE id=?', (report_id,))

    def usage(self, since=None):
        """Totals per role, model and effort; since (Unix seconds) keeps only attempts recorded from then on."""
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('SELECT coalesce(r.role,"conversation") AS role,u.model,u.effort,'
                'count(*) AS attempts,sum(u.input_tokens) AS input_tokens,sum(u.output_tokens) AS output_tokens,'
                'sum(u.input_tokens IS NULL OR u.output_tokens IS NULL) AS unknown_attempts,'
                'sum(u.reasoning_output_tokens) AS reasoning_tokens,sum(u.cached_input_tokens) AS cached_tokens '
                'FROM chat_usage u LEFT JOIN agent_usage_roles r ON r.attempt_id=u.attempt_id '
                'WHERE ? IS NULL OR u.at>=? GROUP BY 1,2,3', (since, since))]
