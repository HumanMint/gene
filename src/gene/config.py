from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db: Path
    guild_id: str
    owner_id: str
    runtime_token: str
    worker_token: str
    deploy_token: str
    workshop_channel_id: str = ''
    history_channel_id: str = ''
    fri_token: str = ''
    runtime_guild_id: str = ''
    agent_channels: str = ''
    agent_history_channels: str = ''
    agent_role_id: str = ''
    test_guild_id: str = ''
    test_channel_id: str = ''
    web_search_enabled: bool = False

    def chat_channel_allowed(self, guild, channel):
        if not guild or not channel:
            return False
        if guild == self.guild_id:
            return channel in {self.workshop_channel_id, *self.agent_channels.split(',')}
        return bool(self.test_guild_id and self.test_channel_id and
                    guild == self.test_guild_id and channel == self.test_channel_id)

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            db=Path(os.environ.get('GENE_DB', '.state/gene.sqlite3')),
            guild_id=os.environ.get('GENE_GUILD_ID', ''),
            owner_id=os.environ.get('GENE_OWNER_ID', ''),
            runtime_token=os.environ.get('GENE_RUNTIME_TOKEN', ''),
            worker_token=os.environ.get('GENE_WORKER_TOKEN', ''),
            deploy_token=os.environ.get('GENE_DEPLOY_TOKEN', ''),
            workshop_channel_id=os.environ.get('GENE_WORKSHOP_CHANNEL_ID', ''),
            history_channel_id=os.environ.get('GENE_HISTORY_CHANNEL_ID', ''),
            fri_token=os.environ.get('GENE_FRI_TOKEN', ''),
            runtime_guild_id=os.environ.get('GENE_RUNTIME_GUILD_ID', ''),
            agent_channels=os.environ.get('FRI_CHANNEL_IDS', ''),
            agent_history_channels=os.environ.get('FRI_HISTORY_CHANNEL_IDS', ''),
            agent_role_id=os.environ.get('FRI_LEARNING_ROLE_ID', ''),
            test_guild_id=os.environ.get('FRI_TEST_GUILD_ID', ''),
            test_channel_id=os.environ.get('FRI_TEST_CHANNEL_ID', ''),
            web_search_enabled=os.environ.get('FRI_WEB_SEARCH_ENABLED', '').lower() == 'true',
        )
        tokens = [settings.runtime_token, settings.worker_token, settings.deploy_token, settings.fri_token]
        if any(len(token) < 32 for token in tokens) or len(set(tokens)) != 4:
            raise ValueError('Four distinct service tokens of at least 32 characters are required')
        return settings

