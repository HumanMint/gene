"""Conservative text minimization before storage or model input."""

import re

PATTERNS = [
    re.compile(r'(?i)\b(?:mfa\.[\w-]{20,}|[\w-]{23,28}\.[\w-]{6}\.[\w-]{25,})\b'),
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,})\b'),
    re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b'),
    re.compile(r'(?i)(?:authorization\s*[:=]\s*(?:bearer|bot)\s+)\S+'),
    re.compile(r'(?im)\b(?:password|passwd|token|api[_ -]?key|secret)\s*[:=]\s*[\'\"]?[^\s\'\"]{6,}'),
    re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S),
]


def redact(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text.replace('\x00', '')


def clean(text: str, limit: int = 6000) -> str:
    return redact(text.strip())[:limit]

