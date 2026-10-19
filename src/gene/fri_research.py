"""Short-lived public research receipts, scoped to one member and conversation."""

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .web_policy import public_url


class ResearchReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=3, max_length=240)
    answer: str = Field(min_length=1, max_length=3500)
    sources: list[str] = Field(min_length=1, max_length=8)
    checked_at: str = Field(max_length=50)

    @model_validator(mode='after')
    def valid_sources(self):
        if not all(public_url(url) for url in self.sources):
            raise ValueError('Research receipts need public provider source URLs')
        if dt.datetime.fromisoformat(self.checked_at).tzinfo is None:
            raise ValueError('Research receipts need a timezone')
        return self


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS web_research (id INTEGER PRIMARY KEY, '
               'guild_id TEXT,user_id TEXT,channel_id TEXT,created REAL,receipt TEXT)')
