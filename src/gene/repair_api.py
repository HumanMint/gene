from fastapi import HTTPException
from pydantic import Field

from .control_api import Scope
from .fri_context import ChatSource
from .repair_store import request


class RepairRequest(Scope):
    source: ChatSource
    revision: int = Field(ge=0)
    body: str = Field(min_length=5, max_length=5500)


def register(app, store, settings, auth):
    def check_scope(body):
        if (body.guild_id != settings.guild_id or
                body.guild_id != (settings.runtime_guild_id or settings.guild_id) or
                not settings.chat_channel_allowed(body.guild_id, body.channel_id)):
            raise HTTPException(403, 'Repair reports stay within the configured Gene server and member DMs')

    @app.post('/repairs', dependencies=[auth('fri')])
    def repair(body: RepairRequest):
        check_scope(body)
        return request(store, body.model_dump())

    @app.post('/repairs/recent', dependencies=[auth('fri')])
    def recent(body: Scope):
        check_scope(body)
        channel = body.conversation_id.split(':')[1]
        if not channel.isdigit():
            raise ValueError('Invalid source channel')
        guild = body.guild_id if body.conversation_id.startswith('public:') else '@me'
        prefix = f'https://discord.com/channels/{guild}/{channel}/'
        with store.connection() as db:
            rows = db.execute('SELECT j.id,j.state,j.result,j.updated FROM jobs j JOIN repair_jobs r '
                'ON r.job_id=j.id WHERE j.guild_id=? AND j.user_id=? AND j.source_url LIKE ? '
                'ORDER BY j.created DESC LIMIT 3', (body.guild_id, body.user_id, prefix+'%')).fetchall()
        return {'repairs': [dict(row) for row in rows]}
