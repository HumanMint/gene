from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Settings
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


class Pause(Actor):
    paused: bool


class Memory(Actor):
    body: str = Field(min_length=1, max_length=1000)
    source_url: str = Field(max_length=200)


class Lease(BaseModel):
    lease: str


class BuildResult(Lease):
    proposal: dict | None = None
    error: str = Field(default='', max_length=6000)


class DeploymentResult(Lease):
    success: bool
    detail: str = Field(max_length=6000)


class UsageReport(Lease):
    attempt_id: str = Field(min_length=1, max_length=100)
    model: str = Field(max_length=100)
    counters: dict


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    store = Store(settings.db)

    @asynccontextmanager
    async def lifespan(app):
        yield

    app = FastAPI(title='Gene controller', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.store = store

    def auth(role: Literal['runtime', 'worker', 'deploy']):
        def verify(authorization: str = Header(default='')):
            expected = getattr(settings, f'{role}_token')
            if not expected or not hmac.compare_digest(authorization, f'Bearer {expected}'):
                raise HTTPException(401, 'Invalid service credentials')
        return Depends(verify)

    def actor(body: Actor, owner=False):
        if not settings.guild_id or body.guild_id != settings.guild_id:
            raise HTTPException(403, 'This server is not configured for Gene')
        if owner and (not settings.owner_id or body.user_id != settings.owner_id):
            raise HTTPException(403, 'Only Gene\'s configured owner can do this')

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

    @app.get('/status', dependencies=[auth('runtime')])
    def status():
        return store.status()

    def public_job(job):
        return {k: v for k, v in job.items() if k not in ('lease', 'heartbeat')}

    @app.get('/requests', dependencies=[auth('runtime')])
    def recent():
        return [public_job(j) for j in store.recent()]

    @app.get('/requests/{job_id}', dependencies=[auth('runtime')])
    def get_job(job_id: str):
        return public_job(store.get(job_id))

    @app.post('/requests', dependencies=[auth('runtime')])
    def request(body: Request):
        actor(body)
        if not settings.workshop_channel_id or body.channel_id != settings.workshop_channel_id:
            raise HTTPException(403, 'Use Gene\'s configured workshop channel')
        return public_job(store.request(body.guild_id, body.user_id, body.source_id,
                                        body.source_url, body.body))

    @app.post('/requests/{job_id}/approve', dependencies=[auth('runtime')])
    def approve(job_id: str, body: Approval):
        actor(body, owner=True)
        store.approve(job_id, body.sha, body.user_id)
        return {'ok': True}

    @app.post('/requests/{job_id}/reject', dependencies=[auth('runtime')])
    def reject(job_id: str, body: Actor):
        actor(body, owner=True)
        store.reject(job_id, body.user_id)
        return {'ok': True}

    @app.post('/pause', dependencies=[auth('runtime')])
    def pause(body: Pause):
        actor(body, owner=True)
        store.set_paused(body.paused, body.user_id)
        return store.status()

    @app.post('/rollback', dependencies=[auth('runtime')])
    def rollback(body: Actor):
        actor(body, owner=True)
        return public_job(store.rollback(body.guild_id, body.user_id))

    @app.post('/usage', dependencies=[auth('runtime')])
    def usage(body: Actor):
        actor(body, owner=True)
        return store.usage()

    @app.post('/memory/list', dependencies=[auth('runtime')])
    def memories(body: Actor):
        actor(body, owner=True)
        return store.memories(body.guild_id)

    @app.post('/memory', dependencies=[auth('runtime')])
    def remember(body: Memory):
        actor(body, owner=True)
        return {'id': store.remember(body.guild_id, body.user_id, body.body, body.source_url)}

    @app.post('/memory/{memory_id}/forget', dependencies=[auth('runtime')])
    def forget(memory_id: str, body: Actor):
        actor(body, owner=True)
        store.forget(memory_id, body.guild_id, body.user_id)
        return {'ok': True}

    @app.post('/privacy/erase', dependencies=[auth('runtime')])
    def erase(body: Actor):
        actor(body)
        store.erase_user(body.user_id)
        return {'ok': True}

    @app.post('/worker/claim', dependencies=[auth('worker')])
    def claim():
        return store.claim()

    @app.post('/worker/{job_id}/heartbeat', dependencies=[auth('worker')])
    def heartbeat(job_id: str, body: Lease):
        store.heartbeat(job_id, body.lease)
        return {'ok': True}

    @app.post('/worker/{job_id}/finish', dependencies=[auth('worker')])
    def finish(job_id: str, body: BuildResult):
        store.finish_build(job_id, body.lease, body.proposal, body.error)
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
