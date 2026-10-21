"""Service-authenticated agent endpoints; owner-only behavior configuration."""

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .fri_agent_store import AgentStore


class Identity(BaseModel):
    guild_id: str
    user_id: str


class Change(Identity):
    version: int = Field(ge=0)
    changes: dict


class Admission(Identity):
    space: str
    channel_id: str = ''


class Context(Identity):
    channel_id: str


class LearningRecord(BaseModel):
    id: str = Field(pattern=r'^\d{1,25}$')
    user_id: str = Field(pattern=r'^\d{1,25}$')
    role_id: str = Field(pattern=r'^\d{1,25}$')
    revision: int = Field(ge=0)
    text: str = Field(max_length=500)
    at: float = Field(ge=0)
    display_name: str = Field(default='', max_length=80)


class ExplorationPlan(BaseModel):
    member_id: str = Field(default='', pattern=r'^\d{0,25}$')
    term: str = Field(max_length=80)
    reason: str = Field(max_length=180)
    member_name: str = Field(default='', max_length=80)


class ExplorationMember(BaseModel):
    user_id: str = Field(pattern=r'^\d{1,25}$')
    role_id: str = Field(pattern=r'^\d{1,25}$')
    name: str = Field(max_length=80)


class ExplorationRoster(BaseModel):
    members: list[ExplorationMember] = Field(min_length=1, max_length=200)


class Checked(BaseModel):
    message_ids: list[str] = Field(max_length=25)


class Finish(BaseModel):
    records: list[LearningRecord] = Field(max_length=40)
    result: dict
    scanned: int = Field(ge=0, le=40)
    before: str | None = Field(default=None, pattern=r'^\d{1,25}$')
    done: bool
    counters: dict
    error: str = Field(default='', max_length=200)
    oldest: float | None = Field(default=None, ge=0)
    newest: float | None = Field(default=None, ge=0)
    newest_id: str | None = Field(default=None, pattern=r'^\d{1,25}$')
    scanned_ids: list[str] = Field(default_factory=list, max_length=25)


def register(app, store, settings, auth, actor):
    agent = AgentStore(store, settings)
    app.state.agent = agent

    @app.get('/agent/state', dependencies=[auth('fri')])
    def state():
        return agent.state()

    @app.post('/agent/settings', dependencies=[auth('fri')])
    def change(body: Change):
        actor(body, owner=True)
        return agent.update(body.changes, body.version, body.user_id)

    @app.post('/agent/admit', dependencies=[auth('fri')])
    def admit(body: Admission):
        if body.guild_id == settings.guild_id:
            actor(body)
        elif body.space != 'public' or not settings.chat_channel_allowed(body.guild_id, body.channel_id):
            raise HTTPException(403, 'Lab chat is limited to the configured public testing channel')
        return agent.admit(body.user_id, body.space, body.guild_id)

    @app.post('/agent/context', dependencies=[auth('fri')])
    def context(body: Context):
        if not settings.chat_channel_allowed(body.guild_id, body.channel_id):
            if not settings.meme_channel_allowed(body.guild_id, body.channel_id):
                raise HTTPException(403, 'Channel is outside Fri scope')
            profile = agent.context(body.channel_id, body.user_id, body.guild_id)
            return {key: profile[key] for key in ('enabled', 'revision')}
        return agent.context(body.channel_id, body.user_id, body.guild_id)

    @app.post('/agent/learning/claim', dependencies=[auth('fri')])
    def claim():
        return agent.claim()

    @app.post('/agent/status-context', dependencies=[auth('fri')])
    def status_context(body: Context):
        if not settings.status_context_allowed(body.guild_id, body.channel_id):
            raise HTTPException(403, 'Channel is outside Fri status context scope')
        profile = agent.context(body.channel_id, body.user_id, body.guild_id)
        return {key: profile[key] for key in ('enabled', 'revision')}

    @app.post('/agent/learning/overview', dependencies=[auth('fri')])
    def overview(body: Identity):
        actor(body, owner=True)
        return agent.overview()

    @app.post('/agent/learning/request', dependencies=[auth('fri')])
    def request_learning(body: Identity):
        actor(body, owner=True)
        return agent.claim(manual=True)

    @app.post('/agent/learning/{batch_id}/finish', dependencies=[auth('fri')])
    def finish(batch_id: str, body: Finish):
        return agent.finish(batch_id, **body.model_dump())

    @app.post('/agent/learning/{batch_id}/plan', dependencies=[auth('fri')])
    def plan(batch_id: str, body: ExplorationPlan):
        return agent.exploration.prepare(batch_id, **body.model_dump())

    @app.post('/agent/learning/{batch_id}/target', dependencies=[auth('fri')])
    def target(batch_id: str, body: ExplorationRoster):
        return agent.exploration.target(batch_id, body.model_dump()['members'])

    @app.post('/agent/learning/{batch_id}/checked', dependencies=[auth('fri')])
    def checked(batch_id: str, body: Checked):
        return agent.exploration.checked(batch_id, body.message_ids)

    @app.get('/agent/reports', dependencies=[auth('fri')])
    def reports():
        return agent.reports()

    @app.post('/agent/reports/{report_id}/view', dependencies=[auth('fri')])
    def view_report(report_id: str, body: Identity):
        actor(body, owner=True)
        report = agent.report(report_id)
        if report is None:
            raise HTTPException(404, 'This learning report expired or was cleared.')
        return report

    @app.post('/agent/reports/{report_id}/delivered', dependencies=[auth('fri')])
    def delivered(report_id: str):
        agent.delivered(report_id)
        return {'ok': True}

    @app.post('/agent/usage', dependencies=[auth('fri')])
    def usage(body: Identity):
        actor(body, owner=True)
        return agent.usage()
