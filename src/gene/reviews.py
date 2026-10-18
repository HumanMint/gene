"""References and routing for explicit owner reviews, independent of model output."""

import hashlib
import json
import re

REVIEW_ID = re.compile(
    r'^gene:review:(?P<phase>i|r):(?P<decision>a|x):(?P<job_id>[a-f0-9]{12}):'
    r'(?P<reference>[a-f0-9]{64}|[a-f0-9]{40})$'
)


def request_reference(job):
    fields = {key: job[key] for key in ('id', 'guild_id', 'user_id', 'source_id', 'source_url', 'body')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pending_review(job, active_sha):
    if job['state'] == 'awaiting_owner':
        return 'intake', request_reference(job)
    proposal = job.get('proposal')
    if job['state'] == 'review' and proposal and proposal['base_sha'] == active_sha:
        return 'release', proposal['sha']
    return None


def owner_dm_component(interaction, bot_id, owner_id):
    """Allow only the owner's review buttons on our own DM messages."""
    if not bot_id or not owner_id or getattr(interaction, 'guild_id', None) is not None:
        return False
    if str(getattr(getattr(interaction, 'user', None), 'id', '')) != str(owner_id):
        return False
    kind = getattr(interaction, 'type', None)
    if getattr(kind, 'value', kind) != 3:
        return False
    message = getattr(interaction, 'message', None)
    if getattr(getattr(message, 'author', None), 'id', None) != bot_id:
        return False
    data = getattr(interaction, 'data', None)
    custom_id = data.get('custom_id') if isinstance(data, dict) else None
    return isinstance(custom_id, str) and REVIEW_ID.fullmatch(custom_id) is not None
