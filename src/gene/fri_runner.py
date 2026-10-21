"""A bounded turn: tools return evidence, never authority for more tools."""

import asyncio
import json

import discord
from redbot.core import commands

from .fri_chat import ChatResult
from .gene_controls import requested

MAX_STEPS = 4
TERMINAL = {'gene_command', 'gene_diagnose', 'run_learning'}


async def run(fri, actions, text, context, result):
    try:
        return await _run(fri, actions, text, context, result)
    except TimeoutError:
        raise commands.UserFeedbackCheckFailure('That turn ran out of time. Saved work remains available; check its status before retrying an action.') from None


async def _run(fri, actions, text, context, result):
    receipts, seen, research = [], set(), None
    offered = dict(context['actions'])
    async with asyncio.timeout(240):
        for _ in range(MAX_STEPS):
            action = result.action
            if not action:
                break
            kind = action['kind']
            fingerprint = json.dumps(action, sort_keys=True)
            if kind not in offered or fingerprint in seen:
                raise commands.UserFeedbackCheckFailure('That step is unavailable or already ran. No action repeated.')
            seen.add(fingerprint)
            if kind == 'gene_command' and not requested(text, action.get('action')):
                raise commands.UserFeedbackCheckFailure('That was not an explicit request to run Gene now.')
            try:
                receipt = await actions.execute(action)
            except (commands.UserFeedbackCheckFailure, discord.HTTPException):
                receipt = {'uncertain': 'Completion was not confirmed. Do not claim success or retry automatically.'}
            receipts.append({'kind': kind, **receipt})
            if kind == 'preview_voice':
                preview = receipt.get('settings_preview')
                return ChatResult(reply='Voice settings preview prepared. Nothing is saved until you click Apply.' if preview
                    else receipt.get('blocked') or receipt.get('uncertain'), status='completed', settings_preview=preview), receipts, research
            if receipt.get('post_preview'):
                return ChatResult(reply='Post preview prepared. Nothing is sent until you click Send once.',
                    status='completed', post_preview=receipt['post_preview']), receipts, research
            if kind == 'web_search' and all(receipt.get(k) for k in ('query', 'answer', 'sources', 'checked_at')):
                research = {k: receipt[k] for k in ('query', 'answer', 'sources', 'checked_at')}
            offered.pop(kind, None)
            if kind in TERMINAL or receipt.get('blocked') or receipt.get('uncertain') or receipt.get('error'):
                offered = {}
            # Reading private member evidence cannot become a persistent draft or a public post.
            if kind in ('read_recent', 'learning_overview', 'run_learning'):
                offered.pop('work_save', None)
                offered.pop('prepare_post', None)
            if len(seen) == MAX_STEPS:
                offered = {}
            result = await fri.chat_once(text, {**context, 'actions': offered,
                'action_result': receipts[-1], 'action_results': list(receipts)})
            if result.action and not offered:
                raise commands.UserFeedbackCheckFailure('The action limit was reached. No further actions were run.')
        if result.action:
            raise commands.UserFeedbackCheckFailure('The action limit was reached. No further actions were run.')
    return result, receipts, research
