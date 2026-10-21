"""Fri-only admission, scoped independently of model-provided action text."""

from pydantic import Field

from .control_api import Scope
from .fri_context import ChatSource
from .fri_operation_store import admit


class Operation(Scope):
    source: ChatSource
    revision: int = Field(ge=0)
    input: str = Field(min_length=1, max_length=4000)
    continuation: bool = False


def register(app, store, settings, auth, chat_scope):
    @app.post('/chat/operations/admit', dependencies=[auth('fri')])
    def operation(body: Operation):
        chat_scope(body)
        if not body.continuation and body.guild_id != settings.guild_id:
            from fastapi import HTTPException
            raise HTTPException(403, 'Gene controls are unavailable from the Lab')
        return admit(store, body.model_dump(), body.source, body.revision, body.input, body.continuation)
