"""Workspace authority belongs to the controller, not generated tool arguments."""

from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .api import SummaryDependency
from .fri_context import ChatSource
from .fri_workspace_store import Workspace


class Scope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    guild_id: str
    user_id: str
    channel_id: str
    conversation_id: str = Field(pattern=r'^(private|public):[0-9]{1,25}$')


class Save(Scope):
    source: ChatSource
    revision: int = Field(ge=0)
    work_id: str = Field(default='', pattern=r'^[a-f0-9]{0,32}$')
    version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=4000)
    state: Literal['open', 'completed', 'cancelled']
    request_text: str = Field(default='', max_length=4000)
    # The member summary's generation, sent by a Fri whose turn gave its model that summary: refused once it moved
    # (fri #171). An older Fri omits it.
    summary_generation: int | None = Field(default=None, ge=0)
    # Other members' summaries the turn may repeat through Fri's earlier replies (fri #216), as a turn sends them: refused
    # once one moved, and kept with the draft. An older Fri omits them.
    summary_dependencies: Annotated[list[SummaryDependency], Field(max_length=200)] = []


class Prepare(Scope):
    source: ChatSource
    revision: int = Field(ge=0)
    work_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    version: int = Field(ge=1)
    target_id: str = Field(pattern=r'^[0-9]{1,25}$')


class Bind(BaseModel):
    message_id: str = Field(pattern=r'^[0-9]{1,25}$')


class Decision(BaseModel):
    source: ChatSource
    preview_id: str = Field(pattern=r'^[0-9]{1,25}$')
    approve: bool


class Lease(BaseModel):
    lease: str = Field(pattern=r'^[a-f0-9]{32}$')


class Finish(Lease):
    state: Literal['sent', 'failed', 'uncertain']
    message_id: str = Field(default='', pattern=r'^[0-9]{0,25}$')


def register(app, store, settings, auth, chat_scope):
    work = Workspace(store)

    def owner(body):
        if (body.user_id != settings.owner_id or body.guild_id != settings.guild_id or
                not body.conversation_id.startswith('private:') or body.source.guild_id is not None):
            raise HTTPException(403, 'One-off posts are available only in the verified owner DM')

    @app.post('/workspace/list', dependencies=[auth('fri')])
    def listing(body: Scope):
        chat_scope(body)
        return work.read(body.model_dump())

    @app.post('/workspace/read/{work_id}', dependencies=[auth('fri')])
    def read(work_id: str, body: Scope):
        chat_scope(body)
        return work.read(body.model_dump(), work_id)

    @app.post('/workspace/save', dependencies=[auth('fri')])
    def save(body: Save):
        chat_scope(body)
        return work.save({k: getattr(body, k) for k in Scope.model_fields}, **body.model_dump(exclude=set(Scope.model_fields) | {'source'}), source=body.source)

    @app.post('/workspace/posts/prepare', dependencies=[auth('fri')])
    def prepare(body: Prepare):
        chat_scope(body)
        owner(body)
        return work.prepare({k: getattr(body, k) for k in Scope.model_fields}, **body.model_dump(exclude=set(Scope.model_fields) | {'source'}), source=body.source)

    @app.get('/workspace/posts/{post_id}', dependencies=[auth('fri')])
    def post(post_id: str):
        return work.post(post_id)

    @app.post('/workspace/posts/{post_id}/bind', dependencies=[auth('fri')])
    def bind(post_id: str, body: Bind):
        work.bind(post_id, body.message_id)
        return {'ok': True}

    @app.post('/workspace/posts/{post_id}/decide', dependencies=[auth('fri')])
    def decide(post_id: str, body: Decision):
        row = work.post(post_id)
        if row['user_id'] != settings.owner_id or row['guild_id'] != settings.guild_id or body.source.user_id != settings.owner_id:
            raise HTTPException(403, 'Owner identity changed or is invalid')
        return work.decide(post_id, body.source, body.preview_id, body.approve)

    @app.post('/workspace/outbox/claim', dependencies=[auth('fri')])
    def claim():
        return work.claim()

    @app.post('/workspace/posts/{post_id}/sending', dependencies=[auth('fri')])
    def sending(post_id: str, body: Lease):
        return work.sending(post_id, body.lease)

    @app.post('/workspace/posts/{post_id}/finish', dependencies=[auth('fri')])
    def finish(post_id: str, body: Finish):
        return work.finish(post_id, **body.model_dump())
