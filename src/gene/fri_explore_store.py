"""Persistent, scoped archive coverage. Plans and historical observations are never authority."""

import datetime as dt
import hashlib
import json
import re
import time

from .privacy import clean

CATEGORIES = ('games', 'music', 'hobbies', 'conversation')
PRIVATE = re.compile(r'(?i)\b(?:password|token|secret|address|diagnos\w*|politic\w*|religio\w*|'
                     r'sexual\w*|health|medical|instructions?|system|developer|permission|approv\w*|deploy|execute)\b')


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fri_explore_queries (
            key TEXT PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL,
            term TEXT NOT NULL, depth INTEGER NOT NULL, before_id TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL, passes INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS fri_explore_seen (
            message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, user_id TEXT NOT NULL, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS fri_explore_members (
            channel_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, at REAL NOT NULL,
            PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS fri_explore_notes (
            channel_id TEXT NOT NULL, user_id TEXT NOT NULL, source_id TEXT NOT NULL,
            category TEXT NOT NULL, topic TEXT NOT NULL, evidence TEXT NOT NULL,
            source_at REAL NOT NULL, learned_at REAL NOT NULL,
            PRIMARY KEY(channel_id,user_id,source_id,category));
    ''')


def notes(db, channel, user=None):
    db.execute('DELETE FROM fri_explore_notes WHERE learned_at<?', (time.time()-90*86400,))
    return [dict(r, source_date=dt.datetime.fromtimestamp(r['source_at'], dt.timezone.utc).date().isoformat()) for r in db.execute(
        'SELECT user_id,source_id,category,topic,evidence,source_at FROM fri_explore_notes '
        'WHERE channel_id=? '+('AND user_id=? ' if user else '')+'ORDER BY learned_at DESC LIMIT ?',
        (channel, user, 8) if user else (channel, 20))]


class Exploration:
    def __init__(self, agent):
        self.agent = agent

    def context(self, db, channel):
        members = [dict(r) for r in db.execute(
            'SELECT m.user_id,m.name,count(s.message_id) AS checked FROM fri_explore_members m '
            'LEFT JOIN fri_explore_seen s ON s.channel_id=m.channel_id AND s.user_id=m.user_id '
            'WHERE m.channel_id=? GROUP BY m.user_id ORDER BY checked,m.at LIMIT 20', (channel,))]
        # Seed discovery from attributed authors already found by the old learning flow.
        row = db.execute('SELECT authors FROM agent_summaries WHERE channel_id=?', (channel,)).fetchone()
        known = {m['user_id'] for m in members}
        for user in (json.loads(row[0]) if row else []):
            if user not in known and len(members) < 20:
                members.append({'user_id': user, 'name': '', 'checked': 0})
        members = [m for m in members if self.enabled(db, m['user_id'])]
        queries = [dict(r) for r in db.execute(
            'SELECT user_id,term,done,passes FROM fri_explore_queries WHERE channel_id=? '
            'ORDER BY at DESC LIMIT 20', (channel,))]
        return {'members': members, 'previous_searches': queries,
                'historical_topics': [{k: n[k] for k in ('user_id', 'topic', 'category', 'source_date')}
                                      for n in notes(db, channel)],
                'checked_messages': db.execute('SELECT count(*) FROM fri_explore_seen WHERE channel_id=?', (channel,)).fetchone()[0]}

    @staticmethod
    def enabled(db, user):
        row = db.execute('SELECT enabled FROM personal_preferences WHERE user_id=?', (user,)).fetchone()
        return row is None or bool(row[0])

    def prepare(self, batch_id, member_id, term, reason, member_name=''):
        with self.agent.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = self.agent.get(db, 'learning_claim')
            if not claim or claim['id'] != batch_id or claim['mode'] != 'explore' or claim.get('privacy_invalidated'):
                raise ValueError('No matching archive exploration claim')
            if claim.get('plan'):
                raise ValueError('This pass already has a search plan')
            allowed = {m['user_id'] for m in claim['exploration']['members']}
            if member_id and (member_id not in allowed or not self.enabled(db, member_id)):
                raise ValueError('Choose a listed eligible member or discovery')
            term = ' '.join(term.split()).casefold()
            if len(term) > 80 or clean(term, 80) != term or PRIVATE.search(term) or any(x in term for x in ('://', '<@', '`')):
                raise ValueError('Choose a short harmless topic to search')
            key = hashlib.sha256(json.dumps([claim['channel_id'], member_id, term, claim['depth']]).encode()).hexdigest()
            old = db.execute('SELECT * FROM fri_explore_queries WHERE key=?', (key,)).fetchone()
            if old and old['done']:
                raise ValueError('This search is already checked; choose another member or topic next pass')
            plan = {'key': key, 'member_id': member_id, 'term': term, 'reason': clean(reason, 180),
                    'member_name': clean(member_name, 80),
                    'before_id': old['before_id'] if old else claim['until_id']}
            claim['plan'] = plan
            self.agent.put(db, 'learning_claim', claim)
            return plan

    def checked(self, batch_id, message_ids):
        with self.agent.store.connection() as db:
            claim = self.agent.get(db, 'learning_claim')
            if not claim or claim['id'] != batch_id or claim['mode'] != 'explore' or not claim.get('plan'):
                raise ValueError('No matching planned exploration')
            if len(message_ids) > claim['limit']:
                raise ValueError('Too many search results')
            return [mid for mid in message_ids if db.execute(
                'SELECT 1 FROM fri_explore_seen WHERE message_id=? AND channel_id=?',
                (mid, claim['channel_id'])).fetchone()]

    def validate(self, claim, scanned, message_ids):
        if not claim.get('plan') or len(message_ids) != scanned or len(set(message_ids)) != scanned:
            raise ValueError('Invalid archive search receipt')
        floor = max(0, int(claim['after']*1000)-1420070400000) << 22
        if any(not isinstance(mid, str) or not mid.isdigit() or len(mid) > 25 or
               not floor <= int(mid) < int(claim['plan']['before_id']) for mid in message_ids):
            raise ValueError('Search results are outside the planned date range')

    def finish(self, db, claim, records, result, message_ids):
        now, channel, plan = time.time(), claim['channel_id'], claim['plan']
        # A short page is NOT exhaustion: Discord can return fewer results for old messages.
        before = str(min(map(int, message_ids))) if message_ids else plan['before_id']
        db.execute('INSERT INTO fri_explore_queries VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(key) '
                   'DO UPDATE SET before_id=excluded.before_id,done=excluded.done,at=excluded.at,passes=passes+1',
                   (plan['key'], channel, plan['member_id'], plan['term'], claim['depth'], before, not message_ids, now))
        fresh = {r['id']: r for r in records.values() if not db.execute(
            'SELECT 1 FROM fri_explore_seen WHERE message_id=?', (r['id'],)).fetchone()}
        saved = []
        for candidate in result.get('observations', [])[:5]:
            if not isinstance(candidate, dict):
                continue
            r = fresh.get(candidate.get('source_id'))
            topic, evidence, category = candidate.get('topic'), candidate.get('evidence'), candidate.get('category')
            if (not r or category not in CATEGORIES or not isinstance(topic, str) or not isinstance(evidence, str)
                    or not 2 <= len(topic) <= 60 or not 5 <= len(evidence) <= 180 or evidence not in r['text']
                    or clean(topic, 60) != topic or clean(evidence, 180) != evidence
                    or PRIVATE.search(topic+' '+evidence) or any(x in topic+evidence for x in ('\n', '`', '<@', '://'))):
                continue
            db.execute('INSERT OR IGNORE INTO fri_explore_notes VALUES (?,?,?,?,?,?,?,?)',
                       (channel, r['user_id'], r['id'], category, topic, evidence, r['at'], now))
            saved.append(f"{r.get('display_name') or r['user_id']}: {topic} (historical discussion)")
        for r in fresh.values():
            db.execute('INSERT INTO fri_explore_seen VALUES (?,?,?,?)', (r['id'], channel, r['user_id'], now))
            db.execute('INSERT INTO fri_explore_members VALUES (?,?,?,?) ON CONFLICT(channel_id,user_id) '
                       'DO UPDATE SET name=excluded.name,at=excluded.at', (channel, r['user_id'], r.get('display_name', ''), now))
            db.execute('DELETE FROM fri_explore_notes WHERE channel_id=? AND user_id=? AND source_id NOT IN '
                       '(SELECT source_id FROM fri_explore_notes WHERE channel_id=? AND user_id=? ORDER BY learned_at DESC LIMIT 8)',
                       (channel, r['user_id'], channel, r['user_id']))
        return saved


def erase(db, user):
    for table in ('fri_explore_seen', 'fri_explore_members', 'fri_explore_notes'):
        db.execute(f'DELETE FROM {table} WHERE user_id=?', (user,))
    # Search terms/reasons may derive from several members; clear them conservatively.
    db.execute('DELETE FROM fri_explore_queries')
    row = db.execute('SELECT value FROM agent_state WHERE key="learning_claim"').fetchone()
    if row and row[0] != 'null':
        claim = json.loads(row[0])
        if claim.get('mode') == 'explore':
            claim.pop('exploration', None)
            claim.pop('plan', None)
            claim['privacy_invalidated'] = True
            db.execute('UPDATE agent_state SET value=? WHERE key="learning_claim"', (json.dumps(claim),))
