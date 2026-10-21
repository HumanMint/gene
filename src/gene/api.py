from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Settings
from .fri_context import ChatSource
from .fri_pending import Clarification
from .fri_research import ResearchReceipt
from .store import Conflict, Store


class Actor(BaseModel):
    guild_id: str
    user_id: str


class Request(Actor):
    source_id: str = Field(min_length=1, max_length=100)
    source_url: str = Field(max_length=200)
    body: str = Field(min_length=5, max_length=6000)
    channel_id: str


class Approval(Actor):
    sha: str


class ReviewDecision(Actor):
    phase: Literal['intake', 'release']
    reference: str = Field(pattern=r'^[a-f0-9]{40}([a-f0-9]{24})?$')
    decision: Literal['approve', 'reject']


class Pause(Actor):
    paused: bool


class Memory(Actor):
    body: str = Field(min_length=1, max_length=1000)
    source_url: str = Field(max_length=200)


class Lease(BaseModel):
    lease: str


class ReviewDelivery(Lease):
    channel_id: str = Field(default='', pattern=r'^[0-9]*$')
    message_id: str = Field(default='', pattern=r'^[0-9]*$')


class Progress(Lease):
    progress: Literal['', 'Coding', 'Testing', 'Repairing tests', 'Publishing proposal',
                      'Verifying fault', 'Independent review'] = ''


class BuildResult(Lease):
    outcome: Literal['failed', 'needs_attention'] = 'failed'
    proposal: dict | None = None
    error: str = Field(default='', max_length=6000)


class DeploymentResult(Lease):
    success: bool
    detail: str = Field(max_length=6000)


class UsageReport(Lease):
    attempt_id: str = Field(min_length=1, max_length=100)
    model: str = Field(max_length=100)
    counters: dict


class ModelSelection(Actor):
    model_id: str = Field(min_length=1, max_length=100)
    effort: str = Field(default='', max_length=20)


class ModelCatalog(BaseModel):
    models: list[dict] = Field(default_factory=list)
    error: str = Field(default='', max_length=500)


class ChatScope(Actor):
    channel_id: str
    conversation_id: str = Field(pattern=r'^(private|public):[0-9]{1,25}$')


class ChatTurn(ChatScope):
    source: ChatSource
    web_receipt: ResearchReceipt | None = None
    clarification: Clarification | None = None
    revision: int = Field(ge=0)
    input: str = Field(min_length=1, max_length=4000)
    reply: str = Field(max_length=3500)
    response_kind: Literal['reply', 'react', 'quiet'] = 'reply'
    allow_closing: bool = False
    reaction: str | None = Field(default=None, max_length=16)
    model: str = Field(max_length=100)
    effort: str = Field(max_length=20)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    notes: list[dict] = Field(default_factory=list, max_length=3)


class ResearchStart(ChatScope):
    source: ChatSource
    revision: int = Field(ge=0)
    input: str = Field(min_length=1, max_length=4000)
    query: str = Field(min_length=3, max_length=240)


class PendingCancel(ChatScope):
    source: ChatSource
    input: str = Field(min_length=1, max_length=4000)


class PersonalControl(ChatScope):
    action: Literal['enable', 'disable']


class ChatUsage(BaseModel):
    attempt_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    model: str = Field(max_length=100)
    effort: str = Field(max_length=20)
    status: Literal['completed', 'failed', 'incomplete', 'interrupted', 'invalid_output']
    counters: dict
    role: Literal['conversation', 'attention', 'summary', 'exploration_plan', 'meme', 'status', 'owner_settings', 'investigation', 'history', 'web_search', 'evaluation'] = 'conversation'


class ChatCatalog(Actor):
    models: list[str] = Field(max_length=500)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = Store(settings.db)

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(title='Gene controller', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.store = store

    def auth(*roles):
        def verify(authorization: str = Header(default='')):
            if not any(getattr(settings, f'{role}_token') and hmac.compare_digest(
                    authorization, 'Bearer ' + getattr(settings, f'{role}_token')) for role in roles):
                raise HTTPException(401, 'Invalid service credentials')
        return Depends(verify)

    def actor(body: Actor, owner=False):
        if not settings.guild_id or body.guild_id != settings.guild_id:
            raise HTTPException(403, 'This server is not configured for Gene')
        if owner and (not settings.owner_id or body.user_id != settings.owner_id):
            raise HTTPException(403, 'Only Gene\'s configured owner can do this')

    def chat_scope(body):
        if not settings.chat_channel_allowed(body.guild_id, body.channel_id):
            raise HTTPException(403, 'Use Fri in a configured channel')
        if body.guild_id != settings.guild_id and body.conversation_id != 'public:'+settings.test_channel_id:
            raise HTTPException(403, 'Lab chat is limited to the configured public testing channel')

    from .fri_agent_api import register
    register(app, store, settings, auth, actor)
    from .control_api import register as register_controls
    register_controls(app, store, settings, auth)
    from .repair_api import register as register_repairs
    register_repairs(app, store, settings, auth)
    from .fri_workspace_api import register as register_workspace
    register_workspace(app, store, settings, auth, chat_scope)
    from .fri_operation_api import register as register_operations
    register_operations(app, store, settings, auth, chat_scope)
    from .fri_social_api import register as register_social
    register_social(app, store, settings, auth, chat_scope)

    @app.exception_handler(Conflict)
    async def conflict_handler(_, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.exception_handler(KeyError)
    async def missing_handler(_, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': 'Request not found'})

    @app.exception_handler(ValueError)
    async def value_handler(_, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': str(exc)})

    @app.get('/health')
    def health():
        store.status()
        return {'ok': True, 'service': 'gene-controller'}

    @app.get('/status', dependencies=[auth('runtime', 'fri')])
    def status():
        return store.status()

    def public_job(job):
        return {**{k: v for k, v in job.items() if k not in ('lease', 'heartbeat')},
                'proposal_current': bool(job.get('proposal') and
                    job['proposal']['base_sha'] == store.status()['active_sha'])}

    @app.get('/requests', dependencies=[auth('fri')])
    def recent():
        return [public_job(j) for j in store.recent(settings.guild_id)]

    @app.get('/requests/{job_id}', dependencies=[auth('fri')])
    def get_job(job_id: str):
        job = store.get(job_id)
        if job['guild_id'] != settings.guild_id:
            raise HTTPException(403, 'Request belongs to a different server')
        return public_job(job)

    @app.post('/requests', dependencies=[auth('fri')])
    def request(body: Request):
        actor(body)
        if body.channel_id not in {settings.workshop_channel_id, *settings.agent_channels.split(',')} or not body.channel_id:
            raise HTTPException(403, 'Use Gene\'s configured workshop channel')
        return public_job(store.request(body.guild_id, body.user_id, body.source_id,
                                        body.source_url, body.body))

    @app.post('/requests/{job_id}/approve', dependencies=[auth('fri')])
    def approve(job_id: str, body: Approval):
        actor(body, owner=True)
        store.approve(job_id, body.sha, body.user_id)
        return {'ok': True}

    @app.post('/requests/{job_id}/review', dependencies=[auth('fri')])
    def review(job_id: str, body: ReviewDecision):
        actor(body, owner=True)
        store.review_decision(job_id, body.phase, body.reference, body.decision, body.user_id)
        return {'ok': True}

    @app.post('/reviews/claim', dependencies=[auth('fri')])
    def claim_review():
        notice = store.claim_review_notice()
        if notice:
            notice['job'] = public_job(notice['job'])
        return notice

    @app.post('/reviews/{job_id}/delivered', dependencies=[auth('fri')])
    def review_delivered(job_id: str, body: ReviewDelivery):
        store.finish_review_notice(job_id, body.lease, body.channel_id, body.message_id)
        return {'ok': True}

    @app.post('/requests/{job_id}/reject', dependencies=[auth('fri')])
    def reject(job_id: str, body: Actor):
        actor(body, owner=True)
        store.reject(job_id, body.user_id)
        return {'ok': True}

    @app.post('/pause', dependencies=[auth('fri')])
    def pause(body: Pause):
        actor(body, owner=True)
        store.set_paused(body.paused, body.user_id)
        return store.status()

    @app.post('/rollback', dependencies=[auth('fri')])
    def rollback(body: Approval):
        actor(body, owner=True)
        return public_job(store.rollback(body.guild_id, body.user_id, body.sha))

    @app.post('/usage', dependencies=[auth('fri')])
    def usage(body: Actor):
        actor(body, owner=True)
        return store.usage()

    @app.post('/memory/list', dependencies=[auth('fri')])
    def memories(body: Actor):
        actor(body, owner=True)
        return store.memories(body.guild_id)

    @app.post('/memory', dependencies=[auth('fri')])
    def remember(body: Memory):
        actor(body, owner=True)
        return {'id': store.remember(body.guild_id, body.user_id, body.body, body.source_url)}

    @app.post('/memory/{memory_id}/forget', dependencies=[auth('fri')])
    def forget(memory_id: str, body: Actor):
        actor(body, owner=True)
        store.forget(memory_id, body.guild_id, body.user_id)
        return {'ok': True}

    @app.post('/privacy/erase', dependencies=[auth('runtime', 'fri')])
    def erase(body: Actor, authorization: str = Header(default='')):
        runtime = hmac.compare_digest(authorization, 'Bearer ' + settings.runtime_token)
        expected = (settings.runtime_guild_id or settings.guild_id) if runtime else settings.guild_id
        if not expected or body.guild_id != expected:
            raise HTTPException(403, 'This server is not configured for this bot')
        store.erase_user(body.user_id)
        return {'ok': True}

    @app.post('/worker/claim', dependencies=[auth('worker')])
    def claim():
        return store.claim()

    @app.post('/chat/context', dependencies=[auth('fri')])
    def chat_context(body: ChatScope):
        chat_scope(body)
        return store.chat_context(body.guild_id, body.conversation_id, body.user_id)

    @app.post('/chat/pending', dependencies=[auth('fri')])
    def chat_pending(body: ChatScope):
        chat_scope(body)
        from .fri_pending import read
        with store.connection() as db:
            row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (body.user_id,)).fetchone()
            pending = read(db, body.guild_id, body.conversation_id, body.user_id, row[0] if row else 0)
        return {'pending': bool(pending)}

    @app.post('/chat/pending/cancel', dependencies=[auth('fri')])
    def chat_pending_cancel(body: PendingCancel):
        chat_scope(body)
        from .fri_pending import cancel
        return cancel(store, body.guild_id, body.conversation_id, body.user_id,
                      body.input, body.source.model_dump())

    @app.post('/chat/turn', dependencies=[auth('fri')])
    def chat_turn(body: ChatTurn):
        chat_scope(body)
        return store.save_chat(body.guild_id, body.conversation_id, body.user_id, body.revision, body.input,
                              body.reply, body.model, body.effort, body.input_tokens, body.output_tokens, body.notes,
                              body.source.model_dump(), body.web_receipt.model_dump() if body.web_receipt else None,
                              body.clarification.model_dump() if body.clarification else None,
                              response_kind=body.response_kind, reaction=body.reaction, allow_closing=body.allow_closing)

    @app.post('/chat/research/start', dependencies=[auth('fri')])
    def research_start(body: ResearchStart):
        chat_scope(body)
        from .fri_research import start
        return start(store, body.guild_id, body.conversation_id, body.user_id, body.revision,
                     body.source.model_dump(), body.input, body.query)

    @app.post('/chat/memory/control', dependencies=[auth('fri')])
    def personal_control(body: PersonalControl):
        chat_scope(body)
        store.personal_control(body.user_id, body.action)
        return {'ok': True}

    @app.post('/chat/usage/report', dependencies=[auth('fri')])
    def chat_usage_report(body: ChatUsage):
        store.add_chat_usage(body.attempt_id, body.model, body.effort, body.status, body.counters, body.role)
        return {'ok': True}

    @app.post('/chat/usage', dependencies=[auth('fri')])
    def chat_usage(body: Actor):
        actor(body, owner=True)
        return store.chat_usage()

    @app.post('/chat/clear', dependencies=[auth('fri')])
    def clear_chat(body: Actor):
        actor(body)
        store.clear_chats(body.user_id)
        return {'ok': True}

    @app.post('/chat/models', dependencies=[auth('fri')])
    def chat_models(body: Actor):
        actor(body, owner=True)
        return store.chat_models()

    @app.post('/chat/models/update', dependencies=[auth('fri')])
    def chat_model_update(body: ChatCatalog):
        actor(body, owner=True)
        store.update_chat_models(body.models)
        return store.chat_models()

    @app.post('/chat/models/select', dependencies=[auth('fri')])
    def chat_model_select(body: ModelSelection):
        actor(body, owner=True)
        store.select_chat_model(body.model_id, body.effort, body.user_id)
        return store.chat_models()

    @app.post('/models', dependencies=[auth('fri')])
    def model_list(body: Actor):
        actor(body, owner=True)
        return store.model_state()

    @app.post('/models/refresh', dependencies=[auth('fri')])
    def model_refresh(body: Actor):
        actor(body, owner=True)
        store.request_model_refresh(body.user_id)
        return store.model_state()

    @app.post('/models/select', dependencies=[auth('fri')])
    def model_select(body: ModelSelection):
        actor(body, owner=True)
        store.select_model(body.model_id, body.effort, body.user_id)
        return store.model_state()

    @app.post('/worker/models/state', dependencies=[auth('worker')])
    def worker_model_state():
        return store.model_state()

    @app.post('/worker/models/update', dependencies=[auth('worker')])
    def worker_model_update(body: ModelCatalog):
        store.update_models(body.models, body.error)
        return store.model_state()

    @app.post('/worker/{job_id}/heartbeat', dependencies=[auth('worker')])
    def heartbeat(job_id: str, body: Progress):
        store.heartbeat(job_id, body.lease, body.progress)
        return {'ok': True}

    @app.post('/worker/{job_id}/finish', dependencies=[auth('worker')])
    def finish(job_id: str, body: BuildResult):
        store.finish_build(job_id, body.lease, body.proposal, body.error, body.outcome)
        return {'ok': True}

    @app.post('/worker/{job_id}/usage', dependencies=[auth('worker')])
    def report_usage(job_id: str, body: UsageReport):
        store.add_usage(job_id, body.lease, body.attempt_id, body.model, body.counters)
        return {'ok': True}

    @app.post('/deploy/claim', dependencies=[auth('deploy')])
    def deploy_claim():
        return store.claim_deploy()

    @app.post('/deploy/{job_id}/finish', dependencies=[auth('deploy')])
    def deploy_finish(job_id: str, body: DeploymentResult):
        store.finish_deploy(job_id, body.lease, body.success, body.detail)
        return {'ok': True}

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host='0.0.0.0', port=8787, access_log=False)


if __name__ == '__main__':
    main()
