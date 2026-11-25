"""Codex JSONL and Claude stream-json accounting. Missing counters remain unknown, never zero."""

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


def _events(lines: Iterable[str]):
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict):
            yield event


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def claude_result(lines: Iterable[str]) -> dict:
    """The final `result` event of a `claude -p --output-format stream-json` run, or {}."""
    return next((e for e in reversed(list(_events(lines))) if e.get('type') == 'result'), {})


def cost_micros(result: dict) -> int | None:
    cost = result.get('total_cost_usd')
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost <= 1000:
        return round(cost * 1_000_000)
    return None


def parse_usage(lines: Iterable[str]) -> Usage:
    totals: dict[str, int] = {}
    unknown: set[str] = set()
    turns = 0
    keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
    for event in _events(lines):
        if event.get('type') == 'turn.completed':
            turns += 1
            counters = event.get('usage') or {}
        elif event.get('type') == 'result':
            # Claude reports one cumulative result per run; its input excludes cache reads and writes.
            raw = event.get('usage') if isinstance(event.get('usage'), dict) else {}
            parts = [_int(raw.get(k)) for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
            details = raw.get('output_tokens_details') if isinstance(raw.get('output_tokens_details'), dict) else {}
            counters = {'input_tokens': None if None in parts else sum(parts),
                        'cached_input_tokens': parts[2], 'output_tokens': raw.get('output_tokens'),
                        'reasoning_output_tokens': details.get('thinking_tokens')}
            turns += _int(event.get('num_turns')) or 1
        else:
            continue
        for key in keys:
            value = _int(counters.get(key))
            if value is not None:
                totals[key] = totals.get(key, 0) + value
            else:
                unknown.add(key)
    return Usage(**{k: totals.get(k) if k not in unknown else None for k in keys}, turns=turns)
