from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .control_store import Controls
from .fri_context import ChatSource
from .fri_conversation import SourceText
from .gene_controls import COMMANDS


class Scope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    guild_id: str
    user_id: str
    channel_id: str
    conversation_id: str = Field(pattern=r'^(private|public):[0-9]{1,25}$')


class Control(Scope):
    source: ChatSource
    request_text: str = Field(min_length=1, max_length=4000)
    target_channel_id: str
    action: str
    argument: str = Field(max_length=240)
    revision: int = Field(ge=0)
    continuation_id: str = Field(default='', pattern=r'^(?:[0-9]{1,25})?$')
    addressed_bot_id: str = Field(default='', pattern=r'^(?:[0-9]{1,25})?$')
    burst_origin: SourceText | None = None
    grounded: bool = False  # The companion kept the argument to the member's own words.


class Poll(BaseModel):
    actions: list[str] = Field(max_length=len(COMMANDS))


class Lease(BaseModel):
    lease: str = Field(pattern=r'^[a-f0-9]{32}$')


class Result(Lease):
    output: str = Field(default='', max_length=3000)
    blocked: str = Field(default='', max_length=300)
    uncertain: str = Field(default='', max_length=300)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    guild_id: str
    sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    started: float = Field(ge=0, allow_inf_nan=False)
    guild_connected: bool
    controls_running: bool


def register(app, store, settings, auth):
    controls = Controls(store)

    def scope(body):
        if (body.guild_id != settings.guild_id or body.guild_id != (settings.runtime_guild_id or settings.guild_id)
                or not settings.chat_channel_allowed(body.guild_id, body.channel_id)):
            raise HTTPException(403, 'Gene controls are available only in its configured server or member DMs')

    @app.get('/controls/catalog', dependencies=[auth('fri')])
    def catalog():
        return {'actions': controls.catalog(), 'dm_channel_id': settings.command_channel_id}

    @app.post('/controls/health', dependencies=[auth('fri')])
    def health(body: Scope):
        scope(body)
        return controls.health()

    @app.post('/runtime/health', dependencies=[auth('runtime')])
    def heartbeat(body: Heartbeat):
        if body.guild_id != (settings.runtime_guild_id or settings.guild_id):
            raise HTTPException(403, 'Wrong runtime server')
        controls.heartbeat(body.model_dump())
        return {'ok': True}

    @app.post('/controls/recent', dependencies=[auth('fri')])
    def recent(body: Scope):
        scope(body)
        # `correctable`: a play of theirs a correction could put right, which the three receipts may not show.
        return {'receipts': controls.recent(body.user_id, body.guild_id, body.conversation_id),
                'correctable': controls.correctable(body.user_id, body.guild_id, body.conversation_id)}

    @app.post('/controls', dependencies=[auth('fri')])
    def enqueue(body: Control):
        scope(body)
        if body.source.guild_id is not None:
            if (body.source.channel_id != body.target_channel_id or
                    body.conversation_id != 'public:'+body.target_channel_id):
                raise HTTPException(403, 'Public controls stay in their source channel')
            # channel_id is the approved parent for a public thread. Gene independently
            # verifies the actual Discord parent before starting; never redirect to it.
        elif (body.target_channel_id != settings.command_channel_id or
              not settings.chat_channel_allowed(body.guild_id, body.target_channel_id)):
            raise HTTPException(403, 'DM commands require the configured bot commands channel')
        if body.action not in controls.catalog():
            raise ValueError('Gene is not currently offering that command')
        return controls.enqueue(body.model_dump())

    @app.post('/controls/{identifier}/result', dependencies=[auth('fri')])
    def result(identifier: str, body: Scope):
        scope(body)
        return controls.result(identifier, body.user_id, body.guild_id, body.conversation_id)

    @app.post('/runtime/controls/poll', dependencies=[auth('runtime')])
    def poll(body: Poll):
        return controls.poll(body.actions)

    @app.post('/runtime/controls/{identifier}/start', dependencies=[auth('runtime')])
    def start(identifier: str, body: Lease):
        controls.start(identifier, body.lease)
        return {'ok': True}

    @app.post('/runtime/controls/{identifier}/finish', dependencies=[auth('runtime')])
    def finish(identifier: str, body: Result):
        controls.finish(identifier, body.lease, body.model_dump(exclude={'lease'}))
        return {'ok': True}
