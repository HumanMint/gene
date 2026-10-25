"""Short-lived event invitations, never channel, member, or action permissions.

The authenticated Discord harness attests the gateway event. The controller checks
that evidence and fences its use; it does not independently contact Discord.
"""

import hashlib
import json
import re
import secrets
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .fri_context import ChatSource, addressed_content
from .privacy import clean

TTL = 300
EVIDENCE_TTL = 7 * 86400
BREADCRUMB_INPUT = '[Explicit mention request; surrounding context not retained]'
BREADCRUMB_REPLY = '[Fri answered this mention; surrounding context not retained]'


class MentionEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    guild_id: str
    user_id: str
    channel_id: str
    conversation_id: str = Field(pattern=r'^public:[0-9]{1,25}$')
    source: ChatSource


class Admission(MentionEvent):
    content: str = Field(min_length=1, max_length=4000)
    bot_id: str = Field(pattern=r'^[0-9]{1,25}$')


class Profiles(MentionEvent):
    mention_grant: str = Field(pattern=r'^[a-f0-9]{64}$')
    evidence: list[ChatSource] = Field(max_length=25)


def explicit_mention(content, bot_id):
    """A literal mention in code, a quotation, or a URL is not an invitation."""
    visible = re.sub(r'(?ms)^[ \t]*>>>.*\Z', '', content)
    visible = re.sub(r'(?m)^[ \t]*>[^\n]*', '', visible)
    visible = re.sub(r'(?s)(`{3,}|~{3,}).*?(?:\1|\Z)', '', visible)
    visible = re.sub(r'(?s)(`+).*?(?:\1|\Z)', '', visible)
    visible = re.sub(r'(?i)https?://\S+', '', visible)
    visible = re.sub(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\'[^\'\n]*\'', '', visible)
    return bool(re.search(r'(?<!\\)<@!?' + re.escape(str(bot_id)) + r'>', visible))


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS mention_grants ('
               'token TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, guild_id TEXT NOT NULL, '
               'channel_id TEXT NOT NULL, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, '
               'source TEXT NOT NULL, input_hashes TEXT NOT NULL, revision INTEGER NOT NULL, '
               'agent_version INTEGER NOT NULL, expires REAL NOT NULL, admission TEXT NOT NULL, '
               'profiles TEXT NOT NULL DEFAULT "{}", '
               'turn_hash TEXT NOT NULL DEFAULT "")')


def _revision(db, user):
    row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
    return row[0] if row else 0


def _agent_version(db):
    from .fri_agent_store import DEFAULTS, AgentStore
    if not {**DEFAULTS, **AgentStore.get(db, 'config', {})}['enabled']:
        raise HTTPException(403, 'Fri conversation is currently disabled')
    return AgentStore.get(db, 'version', 0)


def _event(body):
    source = body.source.check(body.guild_id, body.conversation_id, body.user_id)
    if (source.kind != 'discord_message' or source.guild_id != body.guild_id or
            body.channel_id != source.channel_id or body.conversation_id != 'public:'+source.channel_id):
        raise HTTPException(403, 'Mention invitations stay in their exact server message channel')
    return source


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def check(db, body, *, write=False, text=None):
    """Validate even a supplied grant in an otherwise configured channel."""
    if not body.mention_grant or body.source is None:
        raise HTTPException(403, 'This channel needs a fresh explicit Fri mention')
    source = _event(body)
    agent_version = _agent_version(db)
    row = db.execute('SELECT * FROM mention_grants WHERE token=?', (body.mention_grant,)).fetchone()
    if (not row or row['expires'] < time.time() or any(row[key] != getattr(body, key)
            for key in ('guild_id', 'channel_id', 'conversation_id', 'user_id')) or
            row['source'] != source.model_dump_json() or row['revision'] != _revision(db, body.user_id) or
            row['agent_version'] != agent_version):
        raise HTTPException(403, 'This mention invitation expired or its conversation changed')
    for user, revision in json.loads(row['profiles']).items():
        if revision != _revision(db, user):
            raise HTTPException(403, 'Mention context changed its privacy settings')
    if write and row['turn_hash']:
        raise HTTPException(409, 'This mention event already completed a reply')
    if text is not None and _hash(clean(text, 4000)) not in json.loads(row['input_hashes']):
        raise HTTPException(403, 'Mention text does not match its admitted Discord event')
    return row


def context(store, body):
    # Keep validation and the read in one transaction, including privacy revisions.
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = check(db, body)
        profile = store._personal_context(db, body.guild_id, body.conversation_id, body.user_id)
        receipts = [json.loads(item[0]) for item in db.execute(
            'SELECT receipt FROM web_research WHERE guild_id=? AND channel_id=? AND user_id=? '
            'AND created>? ORDER BY id DESC LIMIT 3',
            (body.guild_id, body.conversation_id, body.user_id, time.time()-86400))]
        return {'pending': None, 'turns': [], 'facts': [], 'revision': row['revision'],
                'web_research': receipts if profile['enabled'] else [],
                'personal_memory': {'enabled': profile['enabled'], 'notes': []},
                'model': store._setting(db, 'chat_model'), 'effort': store._setting(db, 'chat_effort')}


def save_turn(store, body):
    """Persist one neutral receipt, without learning from unapproved surroundings."""
    from .fri_responses import validate_response
    validate_response(body.response_kind, body.reply, body.reaction, notes=body.notes)
    if body.response_kind != 'reply':
        raise HTTPException(403, 'An explicit mention invitation needs a text reply')
    if body.notes or body.burst or body.clarification or body.allow_closing:
        raise HTTPException(403, 'Mention invitations cannot save notes, bursts, or pending actions')
    fingerprint = _hash(body.model_dump_json())
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = check(db, body, text=body.input)
        if body.revision != row['revision']:
            raise HTTPException(403, 'Mention privacy revision does not match')
        if row['turn_hash']:
            if row['turn_hash'] != fingerprint:
                raise HTTPException(409, 'This mention event already completed a different reply')
            # A retry answers as the first save did. A breadcrumb gone since (pruned or erased) reads as not kept.
            kept = db.execute('SELECT 1 FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? AND source=?',
                              (body.guild_id, body.conversation_id, body.user_id, row['source'])).fetchone()
            return {'ok': True, 'first_memory': False, **({} if kept else {'kept': False})}
        if body.web_receipt:
            from .web_policy import public_query
            receipt = body.web_receipt.model_dump()
            # A permit created while this channel was configured can derive
            # from a pending older question. Mention authority is narrower.
            if not public_query(receipt['query'], body.input):
                raise ValueError('Research needs a public topic from this mention request')
        db.execute('UPDATE mention_grants SET turn_hash=? WHERE token=?', (fingerprint, body.mention_grant))
        db.execute('DELETE FROM web_requests WHERE guild_id=? AND channel_id=? AND user_id=? AND source_id=?',
                   (body.guild_id, body.conversation_id, body.user_id, body.source.id))
        from .fri_memory_store import predates
        if (not store._personal_context(db, body.guild_id, body.conversation_id, body.user_id)['enabled'] or
                predates(db, body.guild_id, body.conversation_id, body.user_id, body.source)):
            return {'ok': True, 'first_memory': False, 'kept': False}  # The grant is used; nothing stays.
        if body.web_receipt:
            db.execute('INSERT INTO web_research(guild_id,user_id,channel_id,created,receipt) VALUES (?,?,?,?,?)',
                       (body.guild_id, body.user_id, body.conversation_id, time.time(),
                        body.web_receipt.model_dump_json()))
            db.execute('DELETE FROM web_research WHERE guild_id=? AND user_id=? AND channel_id=? '
                       'AND id NOT IN (SELECT id FROM web_research WHERE guild_id=? AND user_id=? '
                       'AND channel_id=? ORDER BY id DESC LIMIT 3)',
                       (body.guild_id, body.user_id, body.conversation_id,
                        body.guild_id, body.user_id, body.conversation_id))
        db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at,'
                   'input_tokens,output_tokens,source,response_kind,reaction,allow_closing,burst) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (body.guild_id, body.conversation_id, body.user_id, BREADCRUMB_INPUT, BREADCRUMB_REPLY,
                    body.model, body.effort, time.time(), body.input_tokens, body.output_tokens,
                    row['source'], body.response_kind, body.reaction, False, '[]'))
        db.execute('DELETE FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? '
                   'AND id NOT IN (SELECT id FROM chat_turns WHERE guild_id=? AND channel_id=? '
                   'AND user_id=? ORDER BY id DESC LIMIT 6)',
                   (body.guild_id, body.conversation_id, body.user_id,
                    body.guild_id, body.conversation_id, body.user_id))
        return {'ok': True, 'first_memory': False}


def register(app, store, settings, auth):
    @app.post('/chat/mentions/admit', dependencies=[auth('fri')])
    def admit(body: Admission):
        if not settings.guild_id or body.guild_id != settings.guild_id:
            raise HTTPException(403, 'Mention invitations are available only in Fri\'s main server')
        source = _event(body)
        now = time.time()
        if not -5 <= now-source.timestamp().timestamp() <= TTL or not explicit_mention(body.content, body.bot_id):
            raise HTTPException(403, 'Mention invitations need a fresh explicit Fri mention')
        hashes = json.dumps(sorted({_hash(clean(body.content, 4000)),
                                  _hash(clean(addressed_content(body.content, body.bot_id) or 'hey', 4000))}))
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            agent_version = _agent_version(db)
            # Retain expired event tombstones beyond their admissible event age.
            db.execute('DELETE FROM mention_grants WHERE expires<?', (now-600,))
            previous = db.execute('SELECT * FROM mention_grants WHERE source_id=?', (source.id,)).fetchone()
            if previous:
                if (previous['source'] != source.model_dump_json() or previous['input_hashes'] != hashes or
                        previous['revision'] != _revision(db, body.user_id) or
                        previous['agent_version'] != agent_version or previous['expires'] < now or
                        previous['turn_hash']):
                    raise HTTPException(403, 'This Discord event cannot renew its mention invitation')
                return json.loads(previous['admission'])
            allowance = app.state.agent.admit_in(db, body.user_id, 'public', body.guild_id)
            if not allowance['allowed']:
                return allowance
            token = secrets.token_hex(32)
            expires = min(now+TTL, source.timestamp().timestamp()+TTL)
            revision = _revision(db, body.user_id)
            result = {**allowance, 'mention_grant': token, 'revision': revision, 'expires_at': expires}
            db.execute('INSERT INTO mention_grants(token,source_id,guild_id,channel_id,conversation_id,user_id,'
                       'source,input_hashes,revision,agent_version,expires,admission) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (token, source.id, body.guild_id, body.channel_id, body.conversation_id, body.user_id,
                        source.model_dump_json(), hashes, revision, agent_version, expires, json.dumps(result)))
            return result

    @app.post('/chat/mentions/profiles', dependencies=[auth('fri')])
    def profiles(body: Profiles):
        if not settings.guild_id or body.guild_id != settings.guild_id:
            raise HTTPException(403, 'Mention invitations are available only in Fri\'s main server')
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = check(db, body)
            tracked = json.loads(row['profiles'])
            users = {body.user_id}
            for item in body.evidence:
                event = item.check(body.guild_id, body.conversation_id, item.user_id)
                if (event.guild_id != body.guild_id or event.kind != 'discord_message' or
                        not -5 <= time.time()-event.timestamp().timestamp() <= EVIDENCE_TTL):
                    raise HTTPException(403, 'Mention privacy evidence needs a recent same-channel message')
                users.add(event.user_id)
            if len(users) > 26 or len(set(tracked) | users) > 26:
                raise HTTPException(403, 'Mention privacy evidence is limited to 25 nearby authors')
            if row['turn_hash'] and users-set(tracked)-{body.user_id}:
                raise HTTPException(403, 'Completed mention context cannot add new authors')
            result = {}
            for user in users:
                profile = store._personal_context(db, body.guild_id, body.conversation_id, user)
                revision = _revision(db, user)
                tracked[user] = revision
                result[user] = {'enabled': profile['enabled'], 'revision': revision}
            db.execute('UPDATE mention_grants SET profiles=? WHERE token=?',
                       (json.dumps(tracked, sort_keys=True), body.mention_grant))
            return {'profiles': result}
