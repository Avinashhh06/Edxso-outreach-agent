from __future__ import annotations

import re
from datetime import datetime, timezone

NOT_FOUND = "Not Found"
NOT_AVAILABLE = "Not Available"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*", re.UNICODE)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def word_count(text: str) -> int:
    """Words only; emoji and bare punctuation are not counted."""
    return len(_WORD_RE.findall(text or ""))


def is_valid_email(value: str | None) -> bool:
    return bool(value) and bool(EMAIL_RE.match(value.strip()))


def clean(value) -> str:
    """CSV cell -> stripped string ('' for NaN/None)."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in {"nan", "none"} else s
