"""Deterministic, single-package intent recognition for YouTube launch requests."""

from __future__ import annotations

import re

YOUTUBE_PACKAGE_ID = "com.google.android.youtube"
YOUTUBE_OPEN_ACTION = "OPEN_APP"
YOUTUBE_LAUNCH_SELECTOR_ID = "youtube_launch"

_YOUTUBE_OPEN_PATTERNS = (
    re.compile(r"\b(?:open|launch|start)\s+(?:the\s+)?youtube(?:\s+app)?\b", re.I),
    re.compile(r"(?:youtube|ইউটিউব)\s*(?:অ্যাপ(?:টি)?\s*)?(?:খুলে\s*দাও|খোলো|ওপেন\s*করো|চালু\s*করো)", re.I),
)


def is_youtube_open_request(message: object) -> bool:
    """Accept only a small, explicit YouTube-open phrasing set; all else falls back to chat."""
    normalized = " ".join(str(message or "").split())
    return bool(normalized) and len(normalized) <= 256 and any(pattern.search(normalized) for pattern in _YOUTUBE_OPEN_PATTERNS)


def is_valid_certificate_sha256(value: object) -> bool:
    return bool(re.fullmatch(r"[A-Fa-f0-9]{64}", str(value or "").strip()))
