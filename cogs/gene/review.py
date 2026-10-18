"""Owner DM notifications. Decisions always go back through the trusted controller."""

import io
import logging

import discord
from redbot.core import commands

from gene.reviews import REVIEW_ID, owner_dm_component

LOG = logging.getLogger('gene.review')
NO_MENTIONS = discord.AllowedMentions.none()


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_ID):
    def __init__(self, phase, decision, job_id, reference):
        self.phase, self.decision = phase, decision
        self.job_id, self.reference = job_id, reference
        label = 'Reject' if decision == 'x' else ('Allow coding' if phase == 'i' else 'Approve this commit')
        super().__init__(discord.ui.Button(label=label,
            style=discord.ButtonStyle.danger if decision == 'x' else discord.ButtonStyle.primary,
            custom_id=f'gene:review:{phase}:{decision}:{job_id}:{reference}'))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(**match.groupdict())

    async def interaction_check(self, interaction):
        cog = interaction.client.get_cog('Gene')
        return bool(cog and owner_dm_component(interaction, interaction.client.user.id, cog.owner_id))

    async def callback(self, interaction):
        if not await self.interaction_check(interaction):
            return
        cog = interaction.client.get_cog('Gene')
        await interaction.response.defer()
        try:
            await cog.api('POST', f'/requests/{self.job_id}/review', {
                'guild_id': str(cog.guild_id), 'user_id': str(interaction.user.id),
                'phase': 'intake' if self.phase == 'i' else 'release',
                'reference': self.reference, 'decision': 'reject' if self.decision == 'x' else 'approve'})
        except commands.UserFeedbackCheckFailure as exc:
            # Do not erase a newer review that might now occupy this same DM message.
            await interaction.followup.send(str(exc)[:1500], ephemeral=True,
                                            allowed_mentions=NO_MENTIONS)
            return
        if self.decision == 'x':
            text = f'Request `{self.job_id}` rejected.'
        elif self.phase == 'i':
            text = (f'Coding allowed for `{self.job_id}`. The worker will prepare a tested proposal. '
                    'Deployment still needs your approval of its exact commit.')
        else:
            text = (f'Approved `{self.reference}` for request `{self.job_id}`. '
                    'Deployment will run health checks and restore the previous release on failure.')
        await interaction.edit_original_response(content=text, embed=None, view=None,
                                                  allowed_mentions=NO_MENTIONS)


def review_message(notice):
    job, phase, reference = notice['job'], notice['phase'], notice['reference']
    intake = phase == 'intake'
    title = 'Allow this feature or fix to be developed?' if intake else 'Approve this tested release?'
    explanation = ('No coding has started. Allowing coding does not approve a live release.' if intake else
                   'Review the code diff before approving this exact commit. Tests alone do not prove a change is safe.')
    preview = discord.utils.escape_markdown(discord.utils.escape_mentions(job['body']))[:2200]
    embed = discord.Embed(title=title, description=explanation + '\n\n**Submitted request (untrusted)**\n' + preview,
                          colour=0x5865F2)
    embed.add_field(name='Request', value=f"`{job['id']}`", inline=True)
    embed.add_field(name='Submitted by', value=f"<@{job['user_id']}>", inline=True)
    if not intake:
        embed.add_field(name='Commit to deploy', value=f'`{reference}`', inline=False)
    embed.set_footer(text='Read the complete request in the attachment. Request text cannot grant permissions.')
    document = (f"Request: {job['id']}\nSubmitted by: {job['user_id']}\nSource: {job['source_url']}\n\n"
                + job['body'])
    attachment = discord.File(io.BytesIO(document.encode()), filename=f"request-{job['id']}.txt")
    view = discord.ui.View(timeout=None)
    for decision in ('a', 'x'):
        view.add_item(ReviewButton('i' if intake else 'r', decision, job['id'], reference))
    url = (job.get('proposal') or {}).get('url', '')
    if not intake and url.startswith('https://github.com/HumanMint/gene/'):
        view.add_item(discord.ui.Button(label='Review code changes', url=url))
    return embed, attachment, view


async def deliver_review(cog, notice):
    """Send to the configured owner only, or edit that request's previous DM."""
    if not cog.owner_id:
        raise ValueError('Gene owner is not configured')
    owner = cog.bot.get_user(cog.owner_id) or await cog.bot.fetch_user(cog.owner_id)
    channel = await owner.create_dm()
    previous = None
    if str(channel.id) == notice.get('channel_id') and notice.get('message_id'):
        try:
            previous = await channel.fetch_message(int(notice['message_id']))
        except discord.NotFound:
            pass
    embed, attachment, view = review_message(notice)
    try:
        if previous:
            if previous.author.id != cog.bot.user.id:
                raise ValueError('Review message is not authored by Gene')
            return await previous.edit(content=None, embed=embed, attachments=[attachment], view=view,
                                       allowed_mentions=NO_MENTIONS)
        return await channel.send(embed=embed, file=attachment, view=view, allowed_mentions=NO_MENTIONS)
    finally:
        attachment.close()


async def notify_owner(cog):
    notice = await cog.api('POST', '/reviews/claim')
    if not notice:
        return
    payload = {'lease': notice['lease']}
    try:
        message = await deliver_review(cog, notice)
        payload.update(channel_id=str(message.channel.id), message_id=str(message.id))
    except (discord.HTTPException, ValueError, TimeoutError) as exc:
        LOG.warning('Owner review DM unavailable: %s', type(exc).__name__)
    await cog.api('POST', f"/reviews/{notice['job']['id']}/delivered", payload)
