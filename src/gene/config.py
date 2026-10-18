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
        )
        tokens = [settings.runtime_token, settings.worker_token, settings.deploy_token]
        if any(len(token) < 32 for token in tokens) or len(set(tokens)) != 3:
            raise ValueError('Three distinct service tokens of at least 32 characters are required')
        return settings

