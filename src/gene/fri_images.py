"""Explicit current-message still images, never ambient collection or tool authority."""

import re

import discord
import httpx
from redbot.core import commands

from .fri_meme_media import load_image, sources

QUESTION = re.compile(r'(?i)(?:\?|\b(?:what|why|how|explain|describe|read|look|help|identify|'
                      r'compare|analy[sz]e|interpret|screenshot|picture|image)\b)')
CHANGED = ('The image request changed while I was checking it. Any earlier tool actions keep '
           'their recorded outcomes; check their status before retrying. Ask again with the current attachment.')


async def attach(context, event, text):
    """Only an addressed chat turn with a textual image request can supply images."""
    if not isinstance(event, discord.Message) or not QUESTION.search(text):
        return
    attachments = getattr(event, 'attachments', ())
    # In normal chat accept only the requester's own attachments, not arbitrary
    # external embeds, referenced posts, or images from recent channel context.
    if not isinstance(attachments, (list, tuple)) or not attachments:
        return
    if not isinstance(getattr(event, 'embeds', None), (list, tuple)):
        return
    urls = sources(event)
    attachment_urls = [str(a.url) for a in attachments]
    if urls is None or urls != attachment_urls:
        raise commands.UserFeedbackCheckFailure('Attach up to two non-spoiler PNG, JPEG or WebP still images, up to 4 MB each. Videos and GIFs are not supported.')
    try:
        context['images'] = [await load_image(url, event.channel.id) for url in urls]
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise commands.UserFeedbackCheckFailure('I couldn’t inspect that image. Try a small PNG, JPEG or WebP attachment.') from None
    context['image_sources'] = urls


async def unchanged(context, event):
    if not context.get('image_sources'):
        return True
    try:
        current = await event.channel.fetch_message(event.id)
        return (current.author.id == event.author.id and not current.webhook_id and
                current.content == event.content and current.edited_at == event.edited_at and
                sources(current) == context['image_sources'])
    except discord.HTTPException:
        return False


async def require_current(context, event):
    if not await unchanged(context, event):
        raise commands.UserFeedbackCheckFailure(CHANGED)
