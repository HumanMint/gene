"""Service-authenticated social capabilities with controller-side event fences."""

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .fri_context import ChatSource
from .fri_social_store import Social
from .fri_workspace_api import Scope


class Event(Scope):
    source: ChatSource
    revision: int = Field(ge=0)


class Reminder(Event):
    body: str = Field(min_length=1, max_length=1000)
    local_time: str = Field(min_length=16, max_length=19)
    timezone: str = Field(min_length=1, max_length=80)


class Plan(Event):
    title: str = Field(min_length=1, max_length=100)
    local_times: str = Field(min_length=33, max_length=100)
    timezone: str = Field(min_length=1, max_length=80)
    request_text: str = Field(min_length=1, max_length=4000)


class Bind(Scope):
    message_id: str = Field(pattern=r'^[0-9]{1,25}$')


class Decision(Event):
    decision: str = Field(pattern=r'^(confirm|cancel|publish|vote[0-3]|final[0-3]|going|maybe|cannot)$')
    preview_id: str = Field(default='', pattern=r'^[0-9]{0,25}$')


class Lease(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lease: str = Field(pattern=r'^[a-f0-9]{32}$')


class Finish(Lease):
    state: Literal['sent', 'failed', 'uncertain']
    message_id: str = Field(default='', pattern=r'^[0-9]{0,25}$')


def register(app, store, settings, auth, chat_scope):
    social = Social(store)

    def scope(body, kind):
        chat_scope(body)
        if body.guild_id != settings.guild_id or not body.conversation_id.startswith('private:' if kind == 'reminder' else 'public:'):
            raise HTTPException(403, 'Personal reminders require a member DM; game-night cards require an allowed server channel.')
        return {key: getattr(body, key) for key in Scope.model_fields}

    @app.post('/social/reminders/prepare', dependencies=[auth('fri')])
    def reminder_prepare(body: Reminder):
        return social.reminder_prepare(scope(body, 'reminder'), body.source, body.revision, body.body, body.local_time, body.timezone)

    @app.post('/social/reminders/list', dependencies=[auth('fri')])
    def reminder_list(body: Scope):
        return social.reminder_list(scope(body, 'reminder'))

    @app.post('/social/reminders/{item}/read', dependencies=[auth('fri')])
    def reminder_read(item: str, body: Scope):
        with store.connection() as db:
            social.prune(db)
            return social.reminder_row(db, item, scope(body, 'reminder'))

    @app.post('/social/reminders/{item}/bind', dependencies=[auth('fri')])
    def reminder_bind(item: str, body: Bind):
        social.bind('reminder', item, scope(body, 'reminder'), body.message_id)
        return {'ok': True}

    @app.post('/social/reminders/{item}/decide', dependencies=[auth('fri')])
    def reminder_decide(item: str, body: Decision):
        return social.reminder_decide(item, scope(body, 'reminder'), body.source, body.revision, body.decision, body.preview_id)

    @app.post('/social/reminders/claim', dependencies=[auth('fri')])
    def reminder_claim():
        return social.reminder_claim()

    @app.post('/social/reminders/{item}/sending', dependencies=[auth('fri')])
    def reminder_sending(item: str, body: Lease):
        return social.reminder_sending(item, body.lease)

    @app.post('/social/reminders/{item}/finish', dependencies=[auth('fri')])
    def reminder_finish(item: str, body: Finish):
        return social.reminder_finish(item, **body.model_dump())

    @app.post('/social/plans/prepare', dependencies=[auth('fri')])
    def plan_prepare(body: Plan):
        return social.plan_prepare(scope(body, 'plan'), body.source, body.revision, body.title, body.local_times, body.timezone, body.request_text)

    @app.post('/social/plans/{item}/read', dependencies=[auth('fri')])
    def plan_read(item: str, body: Scope):
        with store.connection() as db:
            social.prune(db)
            return social.plan_row(db, item, scope(body, 'plan'))

    @app.post('/social/plans/{item}/bind', dependencies=[auth('fri')])
    def plan_bind(item: str, body: Bind):
        social.bind('plan', item, scope(body, 'plan'), body.message_id)
        return {'ok': True}

    @app.post('/social/plans/{item}/decide', dependencies=[auth('fri')])
    def plan_decide(item: str, body: Decision):
        return social.plan_decide(item, scope(body, 'plan'), body.source, body.revision, body.decision, body.preview_id)
