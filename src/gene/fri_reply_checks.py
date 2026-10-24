"""Reply checks: notice when a member's next message says Fri's last reply failed them.

Chat text never leaves the controller. Fri's model reads recent reply/follow-up pairs and labels the
unhappy ones from fixed lists; only those labels, fenced by every member's privacy revision, are kept.
Export for maintainers: python -m gene.fri_reply_checks export <database> [days]
"""

import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

CATEGORIES = ('wrong_info', 'misunderstood', 'ignored_request', 'failed_action', 'too_long', 'vague',
              'repetitive', 'tone', 'refused', 'other')
SEVERITIES = ('low', 'medium', 'high')
TOPICS = ('games', 'music_playback', 'media_and_shows', 'current_events_research', 'reminders_and_plans',
          'images', 'gene_commands', 'memory_and_privacy', 'settings', 'banter', 'advice', 'tech_help',
          'server_info', 'other')
FIXES = ('verify_facts', 'ask_clarifying_question', 'answer_the_actual_question', 'follow_through_on_action',
         'report_failures_honestly', 'be_shorter', 'be_more_specific', 'avoid_repeating', 'adjust_tone',
         'dont_refuse', 'use_context', 'other')
# Labels only: a model's free text could carry what a member said, so none is accepted.
LABELS = {'category': CATEGORIES, 'severity': SEVERITIES, 'topic': TOPICS, 'fix': FIXES}
WEIGHT = {'low': 1, 'medium': 2, 'high': 3}
FOLLOW_UP = 900  # A member's next turn this soon after Fri's reply is read as a reaction to it.
BATCH = 20
SCAN = 200
ATTEMPTS = 3
CLAIM_TIMEOUT = 600
RETENTION = 30*86400
TEXT = 600  # Per-message characters a model sees.
# Discord markup and ids a reader could resolve to an account, channel or message.
MARKUP = ((re.compile(r'<@!?\d+>'), '@someone'), (re.compile(r'<@&\d+>'), '@role'), (re.compile(r'<#\d+>'), '#channel'),
          (re.compile(r'</([\w -]+):\d+>'), r'/\1'), (re.compile(r'<a?:(\w+):\d+>'), r':\1:'),
          (re.compile(r'https?://(?:[\w-]+\.)?discord(?:app)?\.com/channels/[\w@/]+', re.I), '[discord link]'),
          (re.compile(r'\d{15,}'), '[id]'))
STATE = ('reply_check_since', 'reply_check_cursor', 'reply_check_retry', 'reply_check_claim',
         'reply_check_budget', 'next_reply_check')


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fri_reply_findings (
            id INTEGER PRIMARY KEY, at REAL NOT NULL, turn_at REAL NOT NULL, user_id TEXT NOT NULL,
            revision INTEGER NOT NULL, provenance TEXT NOT NULL, space TEXT NOT NULL,
            category TEXT NOT NULL, severity TEXT NOT NULL, topic TEXT NOT NULL, fix TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fri_reply_check_runs (
            id TEXT PRIMARY KEY, at REAL NOT NULL, pairs INTEGER NOT NULL, flagged INTEGER NOT NULL,
            status TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL);
    ''')


def _state():
    from .fri_agent_store import AgentStore
    return AgentStore.get, AgentStore.put


def prune(db):
    """Housekeeping with the controller health check, so expiry doesn't wait for checks to run."""
    now = time.time()
    db.execute('DELETE FROM fri_reply_findings WHERE at<?', (now-RETENTION,))
    db.execute('DELETE FROM fri_reply_check_runs WHERE at<?', (now-RETENTION,))
    claim = _state()[0](db, 'reply_check_claim')
    if claim and now-claim['at'] >= CLAIM_TIMEOUT:  # Abandoned, e.g. Fri stopped mid-check.
        settle(db, claim, 'interrupted', now)


def start(db, put):
    """Checks (re)started: neither half of a pair may come from before this moment."""
    now = time.time()
    put(db, 'reply_check_since', now)
    put(db, 'reply_check_cursor', [now, 0])
    put(db, 'reply_check_retry', None)
    put(db, 'reply_check_claim', None)  # A batch claimed before the restart can't finish into it.


def erase(db, user_id):
    """Findings and any pending pair go with a privacy change of the member, or of anyone a pair drew on."""
    db.execute("DELETE FROM fri_reply_findings WHERE user_id=? OR EXISTS (SELECT 1 FROM "
               "json_each(fri_reply_findings.provenance) p WHERE json_extract(p.value, '$.user_id')=?)",
               (user_id, user_id))
    get, put = _state()
    claim = get(db, 'reply_check_claim')
    if claim:
        pairs = [pair for pair in claim['pairs'] if pair['user_id'] != user_id and
                 all(item['user_id'] != user_id for item in pair['provenance'])]
        if len(pairs) != len(claim['pairs']):
            put(db, 'reply_check_claim', {**claim, 'pairs': pairs})


def neutral(text):
    """What a model may read of a message: the words, not who or where they point at."""
    for pattern, replacement in MARKUP:
        text = pattern.sub(replacement, text)
    return text[:TEXT]


def settle(db, claim, status, now, flagged=0):
    """Advance past a finished batch; a failed one is retried as the same batch, then skipped.

    The allowance is never refunded: every model call counts, whatever happens to its turns.
    """
    get, put = _state()
    put(db, 'reply_check_claim', None)
    attempts = claim['attempts']+1
    if status == 'completed' or attempts >= ATTEMPTS:
        put(db, 'reply_check_cursor', max(get(db, 'reply_check_cursor') or claim['through'], claim['through']))
        put(db, 'reply_check_retry', None)
    else:
        put(db, 'reply_check_retry', {'through': claim['through'], 'attempts': attempts})
    db.execute('INSERT OR IGNORE INTO fri_reply_check_runs VALUES (?,?,?,?,?,?,?)',
               (claim['id'], now, claim['offered'], flagged, status, claim['model'], claim['effort']))


def current(db, user_id, revision, provenance):
    from .store import Store
    return Store._privacy_state(db, user_id) == (revision, True) and all(
        Store._privacy_state(db, item['user_id']) == (item['revision'], True) for item in provenance)


def recent(db, days):
    """Current findings only: the same privacy fence as everything else derived from chat.

    Unlimited on purpose so totals are exact; retention and the daily pair allowance bound it.
    """
    rows = db.execute('SELECT * FROM fri_reply_findings WHERE at>? ORDER BY at DESC',
                      (time.time()-min(days*86400, RETENTION),)).fetchall()
    return [row for row in rows if current(db, row['user_id'], row['revision'], json.loads(row['provenance']))]


def summarize(db, days):
    """Counts and recurring themes; never who, where or what was said."""
    findings, themes = recent(db, days), {}
    for row in findings:
        theme = themes.setdefault((row['category'], row['topic'], row['fix']), {
            'category': row['category'], 'topic': row['topic'], 'fix': row['fix'], 'count': 0, 'score': 0,
            'severities': {}, 'last_day': ''})
        theme['count'] += 1
        theme['score'] += WEIGHT[row['severity']]
        theme['severities'][row['severity']] = theme['severities'].get(row['severity'], 0)+1
        theme['last_day'] = max(theme['last_day'], time.strftime('%Y-%m-%d', time.gmtime(row['turn_at'])))
    checked = db.execute('SELECT coalesce(sum(pairs),0) FROM fri_reply_check_runs WHERE at>? AND status="completed"',
                         (time.time()-days*86400,)).fetchone()[0]
    return {'days': days, 'checked_pairs': checked, 'flagged': len(findings),
            'categories': _counts(findings, 'category'), 'severities': _counts(findings, 'severity'),
            'spaces': _counts(findings, 'space'),
            'themes': sorted(themes.values(), key=lambda t: (-t['score'], -t['count'], t['category'], t['topic'],
                                                             t['fix']))}


def _counts(findings, key):
    counts = {}
    for row in findings:
        counts[row[key]] = counts.get(row[key], 0)+1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


class Finding(BaseModel):
    pair_id: str = Field(pattern=r'^\d{1,20}$')
    category: str = Field(max_length=40)
    severity: str = Field(max_length=40)
    topic: str = Field(max_length=40)
    fix: str = Field(max_length=40)


class Finish(BaseModel):
    findings: list[Finding] = Field(max_length=BATCH)
    status: str = Field(pattern=r'^(completed|failed|incomplete|interrupted|invalid_output)$')


class ReplyChecks:
    def __init__(self, agent):
        self.agent, self.store, self.settings = agent, agent.store, agent.settings

    def _pairs(self, db, after, since, config, limit, until=None):
        """Fri's verbatim reply followed by the same member's next turn in that conversation.

        Turns are walked by (at, id) so equal timestamps are neither skipped nor repeated. A retry
        stops at the failed batch's end, so new arrivals don't inherit its attempts.
        """
        spaces = ('public:', 'private:') if config['reply_check_private'] else ('public:',)
        bound, args = '', [self.settings.guild_id, after[0], after[0], after[1]]
        if until:
            bound, args = ' AND (at<? OR (at=? AND id<=?))', args+[until[0], until[0], until[1]]
        rows = db.execute('SELECT * FROM chat_turns WHERE guild_id=? AND (at>? OR (at=? AND id>?))'+bound+
                          ' ORDER BY at, id LIMIT ?', (*args, SCAN)).fetchall()
        pairs, through = [], after
        for later in rows:
            if len(pairs) >= limit:
                break
            through = [later['at'], later['id']]
            if not later['channel_id'].startswith(spaces) or not later['attributed'] or not later['input'].strip():
                continue
            earlier = db.execute('SELECT * FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? '
                                 'AND (at<? OR (at=? AND id<?)) ORDER BY at DESC, id DESC LIMIT 1',
                                 (later['guild_id'], later['channel_id'], later['user_id'],
                                  later['at'], later['at'], later['id'])).fetchone()
            if (not earlier or earlier['at'] < since or earlier['response_kind'] != 'reply' or
                    not earlier['verbatim'] or not earlier['attributed'] or
                    not 0 <= later['at']-earlier['at'] <= FOLLOW_UP):
                continue
            revision = earlier['speaker_revision']
            provenance = {(p['user_id'], p['revision']) for turn in (earlier, later)
                          for p in json.loads(turn['provenance'] or '[]')}
            provenance = [{'user_id': user, 'revision': rev} for user, rev in sorted(provenance)]
            if revision is None or later['speaker_revision'] != revision or not current(
                    db, later['user_id'], revision, provenance):
                continue
            pairs.append({'id': str(later['id']), 'earlier_id': earlier['id'], 'user_id': later['user_id'],
                          'revision': revision, 'provenance': provenance, 'turn_at': later['at'],
                          'space': later['channel_id'].split(':')[0],
                          'gap_seconds': int(later['at']-earlier['at']),
                          'earlier': {'member': neutral(earlier['input']), 'fri': neutral(earlier['reply'])},
                          'later': {'member': neutral(later['input']),
                                    'fri': neutral(later['reply']) if later['verbatim'] else ''}})
        if until and len(pairs) < limit:
            through = max(through, until)  # Everything up to the failed batch's end was re-examined.
        return pairs, through, len(rows) < SCAN and len(pairs) < limit

    def claim(self):
        now = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            get, put = self.agent.get, self.agent.put
            config = self.agent.config(db)
            if not config['enabled'] or not config['reply_check_enabled']:
                return None
            pending = get(db, 'reply_check_claim')
            if pending:
                if now-pending['at'] < CLAIM_TIMEOUT:
                    return None
                settle(db, pending, 'interrupted', now)
            if now < get(db, 'next_reply_check', 0):
                return None
            day = int(now//86400)
            budget = get(db, 'reply_check_budget', {'day': day, 'used': 0})
            if budget['day'] != day:
                budget = {'day': day, 'used': 0}
            limit = min(BATCH, config['reply_check_daily_pairs']-budget['used'])
            if limit <= 0:
                put(db, 'next_reply_check', (day+1)*86400)
                return None
            if get(db, 'reply_check_since') is None:  # State purged or never started: start now.
                start(db, put)
            since, retry = get(db, 'reply_check_since'), get(db, 'reply_check_retry')
            after = get(db, 'reply_check_cursor') or [since, 0]
            pairs, through, drained = self._pairs(db, after, since, config, limit, retry and retry['through'])
            put(db, 'next_reply_check', now+(60*config['reply_check_minutes'] if drained and not retry else 120))
            if not pairs:
                put(db, 'reply_check_cursor', through)
                put(db, 'reply_check_retry', None)
                return None
            budget['used'] += len(pairs)
            put(db, 'reply_check_budget', budget)
            claim = {'id': uuid.uuid4().hex, 'at': now, 'through': through, 'offered': len(pairs),
                     'attempts': retry['attempts'] if retry else 0,
                     'model': config['reply_check_model'], 'effort': config['reply_check_effort'],
                     'pairs': [{key: pair[key] for key in ('id', 'earlier_id', 'user_id', 'revision', 'provenance',
                                                           'turn_at', 'space')} for pair in pairs]}
            put(db, 'reply_check_claim', claim)
            return {'id': claim['id'], 'model': claim['model'], 'effort': claim['effort'],
                    'pairs': [{key: pair[key] for key in ('id', 'gap_seconds', 'earlier', 'later')} for pair in pairs]}

    def finish(self, claim_id, findings, status):
        now = time.time()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            claim = self.agent.get(db, 'reply_check_claim')
            if not claim or claim['id'] != claim_id:
                raise ValueError('Reply check expired or already completed')
            # Today's settings decide what is kept, not the ones the batch was claimed under.
            config, since = self.agent.config(db), self.agent.get(db, 'reply_check_since')
            spaces = ('public', 'private') if config['reply_check_private'] else ('public',)
            keep = status == 'completed' and config['enabled'] and config['reply_check_enabled'] and since is not None
            pairs, kept, seen = {pair['id']: pair for pair in claim['pairs']}, 0, set()
            for finding in findings if keep else ():
                pair = pairs.get(finding['pair_id'])
                if (not pair or finding['pair_id'] in seen or pair['space'] not in spaces or
                        any(finding[key] not in allowed for key, allowed in LABELS.items())):
                    continue
                seen.add(finding['pair_id'])
                # Re-read both turns: a pruned or erased turn takes its finding with it.
                turns, first = db.execute('SELECT count(*), min(at) FROM chat_turns WHERE id IN (?,?)',
                                          (pair['earlier_id'], int(pair['id']))).fetchone()
                if turns != 2 or first < since or not current(db, pair['user_id'], pair['revision'],
                                                              pair['provenance']):
                    continue
                db.execute('INSERT INTO fri_reply_findings(at,turn_at,user_id,revision,provenance,space,category,'
                           'severity,topic,fix) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (now, pair['turn_at'], pair['user_id'], pair['revision'], json.dumps(pair['provenance']),
                            pair['space'], *(finding[key] for key in LABELS)))
                kept += 1
            settle(db, claim, status, now, kept)
            return {'kept': kept, 'pairs': claim['offered']}

    def overview(self, days=7):
        """Owner-only. Exact totals; the top themes only, to keep the DM short."""
        with self.store.connection() as db:
            config = self.agent.config(db)
            summary = summarize(db, days)
        return {'enabled': config['enabled'] and config['reply_check_enabled'], **summary,
                'themes': summary['themes'][:5]}


def export(path, days=7):
    """Maintainer read of the labelled findings: no member, channel, message or text."""
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        return {'generated_at': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), **summarize(db, days)}
    finally:
        db.close()


def purge(path):
    """Run before rolling the controller back to a release that can't erase findings."""
    db = sqlite3.connect(path)
    try:
        with db:
            db.execute('DROP TABLE IF EXISTS fri_reply_findings')
            db.execute('DROP TABLE IF EXISTS fri_reply_check_runs')
            # An older controller would keep serving these settings and Fri would keep asking for checks.
            row = db.execute("SELECT value FROM agent_state WHERE key='config'").fetchone()
            if row:
                config = {k: v for k, v in json.loads(row[0]).items() if not k.startswith('reply_check_')}
                db.execute("UPDATE agent_state SET value=? WHERE key='config'", (json.dumps(config),))
            db.execute(f"DELETE FROM agent_state WHERE key IN ({','.join('?'*len(STATE))})", STATE)
    finally:
        db.close()


def register(app, agent, auth, actor, Identity):
    checks = ReplyChecks(agent)
    app.state.reply_checks = checks

    @app.post('/agent/reply-checks/claim', dependencies=[auth('fri')])
    def claim():
        return checks.claim()

    @app.post('/agent/reply-checks/overview', dependencies=[auth('fri')])
    def overview(body: Identity):
        actor(body, owner=True)
        return checks.overview()

    @app.post('/agent/reply-checks/{claim_id}/finish', dependencies=[auth('fri')])
    def finish(claim_id: str, body: Finish):
        if not re.fullmatch(r'[a-f0-9]{32}', claim_id):
            raise HTTPException(404, 'Reply check not found')
        return checks.finish(claim_id, [f.model_dump() for f in body.findings], body.status)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (2, 3) or argv[0] not in ('export', 'purge'):
        raise SystemExit('usage: python -m gene.fri_reply_checks export <database> [days] | purge <database>')
    if argv[0] == 'purge':
        purge(argv[1])
        print('Reply check findings and settings removed.')
        return
    days = int(argv[2]) if len(argv) == 3 else 7
    if not 1 <= days <= 30:
        raise SystemExit('days must be between 1 and 30')
    print(json.dumps(export(argv[1], days), indent=2))


if __name__ == '__main__':
    main()
