from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .fri_context import ChatSource
from .fri_conversation import SourceText
from .fri_pending import Clarification
from .fri_protocol import PROTOCOL, contract
from .fri_research import ResearchReceipt
from .store import Conflict, Store


class Actor(BaseModel):
    guild_id: str
    user_id: str


class TurnSource(BaseModel):
    """Another member whose message shaped this reply, and the privacy revision Fri observed."""
    model_config = ConfigDict(extra='forbid')
    user_id: str = Field(pattern=r'^[0-9]{1,25}$')
    revision: int = Field(ge=0)


class Request(Actor):
    source_id: str = Field(min_length=1, max_length=100)
    source_url: str = Field(max_length=200)
    body: str = Field(min_length=5, max_length=6000)
    channel_id: str
    # Omitted only by a companion that doesn't track sources: the request is then unattributed.
    provenance: Annotated[list[TurnSource], Field(max_length=200)] | None = None
    revision: int | None = Field(default=None, ge=0)  # The requester's, when submitted from a chat draft.


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


class RequesterNoticeResult(Lease):
    guild_id: str | None = None  # Both omitted only by a Fri from before the owner-scoped finish.
    user_id: str | None = None
    state: Literal['sent', 'failed', 'uncertain']
    message_id: str = Field(default='', pattern=r'^[0-9]{0,25}$')


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
    # Sent only by a deployer that knows it: Red was stopped and no release, restore included, proved healthy.
    runtime_unknown: bool = False

    @model_validator(mode='after')
    def known_when_healthy(self):
        if self.success and self.runtime_unknown:
            raise ValueError('A release that proved healthy is a known runtime')
        return self


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
    mention_grant: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    source: ChatSource | None = None
    # Sent by a Fri that understands member profiles. An older Fri gets this conversation's notes only.
    profiles: bool = False
    # Sent by a Fri that understands the member summary. An older Fri never gets it.
    summaries: bool = False


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
    burst: list[SourceText] = Field(default_factory=list, max_length=5)
    # Omitted only by a legacy companion: its saved replies are then marked unattributed.
    provenance: Annotated[list[TurnSource], Field(max_length=200)] | None = None
    reply_message_id: str | None = Field(default=None, pattern=r'^[0-9]{1,25}$')
    verbatim: bool = False  # The saved reply is what Discord showed, not a neutral breadcrumb.
    # server: said where everyone in the server could read it, so it may reach the member's profile
    # and their other conversations. Only Fri can tell, from Discord's permissions at the time.
    audience: Literal['server', 'conversation'] = 'conversation'
    # The member summary's generation from /chat/context, sent by a Fri that gave its model that summary. A withdrawal
    # that drops the summary moves it, and the turn is refused (gene #91). An older Fri omits it: checked as before.
    summary_generation: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def private_stays_private(self):
        if self.audience == 'server' and (self.mention_grant or not self.conversation_id.startswith('public:')):
            raise ValueError('Only an everyone-readable server conversation can reach the member profile')
        if self.audience == 'server' and self.provenance is None:
            raise ValueError('A turn that reaches the member profile needs to say whose words it drew on')
        return self


class MessageSources(BaseModel):
    model_config = ConfigDict(extra='forbid')
    guild_id: str = Field(pattern=r'^[0-9]{1,25}$')
    message_ids: list[Annotated[str, Field(pattern=r'^[0-9]{1,25}$')]] = Field(min_length=1, max_length=10)


class TurnWithdrawal(Actor):
    """A member's message edited or deleted after Fri saved their exchange with it (fri #129)."""
    model_config = ConfigDict(extra='forbid')
    channel_id: str
    conversation_id: str = Field(pattern=r'^(private|public):[0-9]{1,25}$')
    message_id: str = Field(pattern=r'^[0-9]{1,25}$')


class SavedWithdrawal(BaseModel):
    """Members' messages edited or deleted that Fri didn't know it saved an exchange with (fri #174)."""
    model_config = ConfigDict(extra='forbid')
    guild_id: str = Field(pattern=r'^[0-9]{1,25}$')
    message_ids: list[Annotated[str, Field(pattern=r'^[0-9]{1,25}$')]] = Field(min_length=1, max_length=10)


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


class SummaryEdit(ChatScope):
    source: ChatSource
    revision: int = Field(ge=0)
    action: Literal['remove', 'replace']
    text: str = Field(default='', max_length=500)


class PersonalNoteEdit(ChatScope):
    source: ChatSource
    revision: int = Field(ge=0)
    action: Literal['remove', 'replace', 'share', 'keep']
    evidence: str = Field(default='', max_length=180)


class ChatUsage(BaseModel):
    attempt_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    model: str = Field(max_length=100)
    effort: str = Field(max_length=20)
    status: Literal['completed', 'failed', 'incomplete', 'interrupted', 'invalid_output']
    counters: dict
    role: Literal['conversation', 'attention', 'summary', 'exploration_plan', 'meme', 'status', 'owner_settings', 'investigation', 'history', 'web_search', 'image_lookup', 'evaluation', 'reply_check'] = 'conversation'
    # Narrows a role for reporting. Older controllers ignore it and count the role, so a receipt
    # queued before a rollback still delivers.
    purpose: Literal['reply_check'] | None = None


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
        def verify(authorization: str = Header(default=''), x_fri_contract: str = Header(default='')):
            if not any(getattr(settings, f'{role}_token') and hmac.compare_digest(
                    authorization, 'Bearer ' + getattr(settings, f'{role}_token')) for role in roles):
                raise HTTPException(401, 'Invalid service credentials')
            if x_fri_contract and x_fri_contract != PROTOCOL:
                raise HTTPException(409, 'Unsupported Fri controller contract')
        return Depends(verify)

    def actor(body: Actor, owner=False):
        if not settings.guild_id or body.guild_id != settings.guild_id:
            raise HTTPException(403, 'This server is not configured for Gene')
        if owner and (not settings.owner_id or body.user_id != settings.owner_id):
            raise HTTPException(403, 'Only Gene\'s configured owner can do this')

    def chat_scope(body):
        if getattr(body, 'mention_grant', None):
            raise HTTPException(403, 'Mention invitations do not authorize this action')
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
    from .fri_mentions import register as register_mentions
    register_mentions(app, store, settings, auth)

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

    @app.get('/fri/contract', dependencies=[auth('fri')])
    def fri_contract():
        return contract()

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
                                        body.source_url, body.body,
                                        None if body.provenance is None else [item.model_dump() for item in body.provenance],
                                        body.revision))

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

    def requester_notice_scope(job_id, body):
        """Claiming and recording the reply are both the owner's, for a request on this server."""
        actor(body, owner=True)
        if store.get(job_id)['guild_id'] != settings.guild_id:
            raise HTTPException(403, 'Request belongs to a different server')

    @app.post('/requests/{job_id}/requester-notice', dependencies=[auth('fri')])
    def requester_notice(job_id: str, body: Actor):
        """The owner's tap on a shipped repair's DM: one reply to the member who reported it."""
        requester_notice_scope(job_id, body)
        from .requester_notices import claim
        return claim(store, job_id, body.user_id)

    @app.post('/requests/{job_id}/requester-notice/finish', dependencies=[auth('fri')])
    def requester_notice_finish(job_id: str, body: RequesterNoticeResult):
        if body.guild_id is None and body.user_id is None:
            # A Fri-only rollback: its claim was already the owner's, and the lease is that claim's.
            if store.get(job_id)['guild_id'] != settings.guild_id:
                raise HTTPException(403, 'Request belongs to a different server')
        else:
            requester_notice_scope(job_id, body)
        from .requester_notices import finish
        finish(store, job_id, body.lease, body.state, body.message_id)
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
        if body.mention_grant:
            actor(body)
            from .fri_mentions import context
            return context(store, body)
        chat_scope(body)
        return store.chat_context(body.guild_id, body.conversation_id, body.user_id, body.profiles, body.summaries)

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
        if body.mention_grant:
            actor(body)
            from .fri_mentions import save_turn
            return save_turn(store, body)
        chat_scope(body)
        return store.save_chat(body.guild_id, body.conversation_id, body.user_id, body.revision, body.input,
                              body.reply, body.model, body.effort, body.input_tokens, body.output_tokens, body.notes,
                              body.source.model_dump(), body.web_receipt.model_dump() if body.web_receipt else None,
                              body.clarification.model_dump() if body.clarification else None,
                              response_kind=body.response_kind, reaction=body.reaction, allow_closing=body.allow_closing,
                              burst=[row.model_dump() for row in body.burst],
                              provenance=None if body.provenance is None else [row.model_dump() for row in body.provenance],
                              reply_message_id=body.reply_message_id, verbatim=body.verbatim, audience=body.audience,
                              summary_generation=body.summary_generation)

    @app.post('/chat/turn/check', dependencies=[auth('fri')])
    def chat_turn_check(body: ChatTurn):
        # Fri checks before delivering and saves only after delivery succeeds.
        if body.mention_grant:
            raise HTTPException(400, 'Invitation turns are fenced by their mention grant')
        chat_scope(body)
        return store.check_chat(body.user_id, body.revision, [row.model_dump() for row in body.provenance or []],
                                guild_id=body.guild_id, messages=[body.source.id, *(row.source.id for row in body.burst)],
                                summary_generation=body.summary_generation)

    @app.post('/chat/turn/withdraw', dependencies=[auth('fri')])
    def chat_turn_withdraw(body: TurnWithdrawal):
        chat_scope(body)
        return store.withdraw_turn(body.guild_id, body.conversation_id, body.user_id, body.message_id)

    @app.post('/chat/turn/withdraw/saved', dependencies=[auth('fri')])
    def chat_turn_withdraw_saved(body: SavedWithdrawal):
        if body.guild_id != settings.guild_id and not (settings.test_guild_id and body.guild_id == settings.test_guild_id):
            raise HTTPException(403, 'Use Fri in a configured server')
        return store.withdraw_saved(body.guild_id, body.message_ids)

    @app.post('/chat/messages/sources', dependencies=[auth('fri')])
    def chat_message_sources(body: MessageSources):
        return {'messages': store.message_sources(body.guild_id, body.message_ids)}

    @app.post('/chat/research/start', dependencies=[auth('fri')])
    def research_start(body: ResearchStart):
        if body.mention_grant:
            actor(body)
        else:
            chat_scope(body)
        from .fri_research import start
        return start(store, body.guild_id, body.conversation_id, body.user_id, body.revision,
                     body.source.model_dump(), body.input, body.query, mention=body if body.mention_grant else None)

    @app.post('/chat/memory/control', dependencies=[auth('fri')])
    def personal_control(body: PersonalControl):
        chat_scope(body)
        store.personal_control(body.user_id, body.action)
        return {'ok': True}

    @app.post('/chat/memory/notes', dependencies=[auth('fri')])
    def personal_notes(body: ChatScope):
        chat_scope(body)
        from .fri_memory_store import entries
        return entries(store, body.guild_id, body.conversation_id, body.user_id, body.profiles, body.summaries)

    @app.post('/chat/memory/summary', dependencies=[auth('fri')])
    def edit_summary(body: SummaryEdit):
        chat_scope(body)
        from .fri_member_summary import edit
        return edit(store, body.guild_id, body.conversation_id, body.user_id, body.revision, body.source.model_dump(),
                    body.action, body.text)

    @app.post('/chat/memory/notes/{identifier}', dependencies=[auth('fri')])
    def edit_personal_note(identifier: int, body: PersonalNoteEdit):
        chat_scope(body)
        from .fri_memory_store import edit
        return edit(store, body.guild_id, body.conversation_id, body.user_id, identifier,
                    body.revision, body.source.model_dump(), body.action, body.evidence, profiles=body.profiles)

    @app.post('/chat/usage/report', dependencies=[auth('fri')])
    def chat_usage_report(body: ChatUsage):
        store.add_chat_usage(body.attempt_id, body.model, body.effort, body.status, body.counters,
                             body.purpose or body.role)
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
        store.finish_deploy(job_id, body.lease, body.success, body.detail, body.runtime_unknown)
        return {'ok': True}

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host='0.0.0.0', port=8787, access_log=False)


if __name__ == '__main__':
    main()
