"""Codex JSONL accounting. Missing counters remain unknown, never zero."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    turns: int = 0


def parse_usage(lines: Iterable[str]) -> Usage:
    totals: dict[str, int] = {}
    unknown: set[str] = set()
    turns = 0
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get('type') != 'turn.completed':
            continue
        turns += 1
        counters = event.get('usage') or {}
        for key in keys:
            value = counters.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[key] = totals.get(key, 0) + value
            else:
                unknown.add(key)
    return Usage(**{k: totals.get(k) if k not in unknown else None for k in keys}, turns=turns)

