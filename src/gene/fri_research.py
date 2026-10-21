"""Short-lived public research receipts, scoped to one member and conversation."""

import datetime as dt
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .web_policy import public_url

REQUEST_TTL = 600


class ResearchReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=3, max_length=240)
    answer: str = Field(min_length=1, max_length=3500)
    sources: list[str] = Field(min_length=1, max_length=8)
    checked_at: str = Field(max_length=50)

    @model_validator(mode='after')
    def valid_sources(self):
        if not all(public_url(url) for url in self.sources):
            raise ValueError('Research receipts need public provider source URLs')
        if dt.datetime.fromisoformat(self.checked_at).tzinfo is None:
            raise ValueError('Research receipts need a timezone')
        return self


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS web_research (id INTEGER PRIMARY KEY, '
               'guild_id TEXT,user_id TEXT,channel_id TEXT,created REAL,receipt TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS web_requests (guild_id TEXT, channel_id TEXT, user_id TEXT, '
               'source_id TEXT, revision INTEGER, created REAL, query TEXT, '
               'PRIMARY KEY(guild_id,channel_id,user_id,source_id))')


def start(store, guild, conversation, user, revision, source, text, query):
    """A receipt permit for one genuine turn; never a command permission."""
    from .fri_context import ChatSource
    from .fri_pending import read, research_query
    event = ChatSource.model_validate(source).check(guild, conversation, user)
    if not -5 <= time.time()-event.timestamp().timestamp() <= 300:
        raise ValueError('Research needs a fresh request')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        if revision != (row[0] if row else 0):
            raise ValueError('Conversation changed before research started')
        pending = read(db, guild, conversation, user, revision)
        valid = research_query(query, text, pending) == query
        if pending and pending.get('goal') == 'web' and '; clarification: ' in query:
            valid = research_query(query.split('; clarification: ', 1)[1], text, pending) == query
        if not valid:
            raise ValueError('Research needs the current public question or its pending clarification')
        db.execute('DELETE FROM web_requests WHERE created<?', (time.time()-REQUEST_TTL,))
        old = db.execute('SELECT query,revision FROM web_requests WHERE guild_id=? AND channel_id=? '
                         'AND user_id=? AND source_id=?', (guild, conversation, user, event.id)).fetchone()
        if old:
            if old['query'] != query or old['revision'] != revision:
                raise ValueError('This research turn already has a different topic')
            return {'ok': True}
        db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)',
                   (guild, conversation, user, event.id, revision, time.time(), query))
    return {'ok': True}


def authorized_receipt(db, guild, conversation, user, revision, source, query):
    if not source:
        return False
    return bool(db.execute('SELECT 1 FROM web_requests WHERE guild_id=? AND channel_id=? AND user_id=? '
        'AND source_id=? AND revision=? AND query=? AND created>?',
        (guild, conversation, user, source.id, revision, query, time.time()-REQUEST_TTL)).fetchone())
