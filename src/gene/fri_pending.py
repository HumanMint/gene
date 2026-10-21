"""Short-lived clarifications, scoped to the requesting member and actual channel.

An assistant question is never permission. Music continuations additionally need
the original member's explicit command and are consumed atomically by Controls.
"""

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .control_store import source_key, source_used
from .fri_context import ChatSource, addressed_content
from .gene_controls import arguments, requested
from .privacy import clean
from .web_policy import public_query

TTL = 300


class Clarification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: Literal['play', 'volume', 'web']
    question: str = Field(min_length=3, max_length=300)
    topic: str = Field(default='', max_length=240)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS chat_pending (guild_id TEXT, conversation_id TEXT, '
               'user_id TEXT, source_id TEXT, created REAL, revision INTEGER, body TEXT, '
               'PRIMARY KEY(guild_id,conversation_id,user_id))')


def cancelled(text):
    # Song titles are data, including "Hold On" or "Nevermind". Existing explicit
    # controls keep their normal path; only a standalone cancellation aborts early.
    if any(requested(text, verb) for verb in ('play', 'volume', 'pause', 'resume', 'skip', 'stop')):
        return False
    return bool(re.fullmatch(
        r"\s*(?:(?:<@!?\d+>|fri|hey|yo|bro|ok|okay|actually|wait|nah|no|please)[,\s]+)*"
        r"(?:never\s?mind|nvm|cancel(?: (?:it|that|this|my request|the request))?|"
        r"forget (?:it|that|this)|leave it|hold (?:on|off)|not now|"
        r"(?:do not|don't|dont) play\b.*|no|nah|nope|stop)"
        r"(?: (?:please|thanks|for now))?[.!?\s]*", text, re.I))


def answer_text(text, bot_id=''):
    return ' '.join(addressed_content(text, bot_id).split())


def read(db, guild, conversation, user, revision):
    db.execute('DELETE FROM chat_pending WHERE created<?', (time.time()-TTL,))
    row = db.execute('SELECT * FROM chat_pending WHERE guild_id=? AND conversation_id=? AND user_id=? '
                     'AND revision=?', (guild, conversation, user, revision)).fetchone()
    return json.loads(row['body']) if row else None


def clear(db, guild, conversation, user):
    db.execute('DELETE FROM chat_pending WHERE guild_id=? AND conversation_id=? AND user_id=?',
               (guild, conversation, user))


def cancel(store, guild, conversation, user, text, source):
    source = ChatSource.model_validate(source).check(guild, conversation, user)
    if (not cancelled(text) or source.kind != 'discord_message' or
            not -5 <= time.time()-source.timestamp().timestamp() <= TTL):
        raise ValueError('Cancellation needs a fresh explicit Discord message')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        pending = read(db, guild, conversation, user, row[0] if row else 0)
        if not pending or int(source.id) <= int(pending['source']['id']):
            return {'cancelled': False}
        clear(db, guild, conversation, user)
        # Retire its permission even if an older inference finishes after cancellation.
        db.execute('INSERT OR IGNORE INTO gene_control_origins VALUES (?,?)',
                   (source_key(guild, pending['source']['id']), time.time()))
        return {'cancelled': True}


def save(db, guild, conversation, user, revision, text, source, clarification):
    clear(db, guild, conversation, user)
    if not clarification:
        return
    value = Clarification.model_validate(clarification).model_dump()
    if (not source or source.kind != 'discord_message' or
            not -5 <= time.time()-source.timestamp().timestamp() <= TTL):
        raise ValueError('Clarifications need a fresh Discord message')
    if clean(value['question'], 300) != value['question']:
        raise ValueError('Clarification contains private identifiers')
    if value['goal'] == 'web':
        if not public_query(value['topic'], text):
            raise ValueError('A research clarification needs a public topic from this request')
    elif not requested(text, value['goal']) or value['topic']:
        raise ValueError('A music clarification needs an explicit original command')
    if db.execute("SELECT 1 FROM chat_turns WHERE user_id=? AND json_extract(NULLIF(source,''),'$.id')=?",
                  (user, source.id)).fetchone():
        raise ValueError('This message already has a reply')
    if source_used(db, guild, source.id):
        raise ValueError('This request was already handled')
    body = {**value, 'source': source.model_dump(), 'request_text': text, 'revision': revision}
    db.execute('INSERT INTO chat_pending VALUES (?,?,?,?,?,?,?)',
               (guild, conversation, user, source.id, source.timestamp().timestamp(), revision, json.dumps(body)))


def completion(pending, text, action, argument, source, guild, conversation, user, revision, bot_id=''):
    """Pure shared gate; runtime also refetches both real public Discord messages."""
    if not pending or action not in ('play', 'volume') or pending.get('goal') != action or cancelled(text):
        return False
    try:
        origin = ChatSource.model_validate(pending['source']).check(guild, conversation, user)
        current = ChatSource.model_validate(source).check(guild, conversation, user)
        return (origin.kind == current.kind == 'discord_message' and origin.guild_id == current.guild_id
                and 0 < int(current.id)-int(origin.id) and pending['revision'] == revision
                and -5 <= time.time()-origin.timestamp().timestamp() <= TTL
                and -5 <= time.time()-current.timestamp().timestamp() <= TTL
                and requested(pending['request_text'], action)
                and ' '.join(argument.split()).casefold() == answer_text(text, bot_id).casefold()
                and bool(arguments(action, argument)))
    except (ValueError, KeyError, TypeError):
        return False


def research_query(query, message, pending=None):
    """Export only an exact current excerpt or the pending public question.

    Neither the assistant's question nor ambient/private context is exported.
    The isolated researcher can rewrite this topic using public sources.
    """
    if pending and pending.get('goal') == 'web' and not cancelled(message):
        topic = pending['topic']
        if public_query(topic, pending['request_text']):
            if query == topic:
                return topic
            if public_query(query, message) and len(topic)+len(query) < 220:
                return topic+'; clarification: '+query
    return query if public_query(query, message) else None
