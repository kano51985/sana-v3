import re


PAUSE_RE = re.compile(r"<\s*pause\b([^>]*)>", re.IGNORECASE)
_MS_RE = re.compile(r"\bms\s*=\s*[\"']?(\d+)[\"']?", re.IGNORECASE)

DEFAULT_DELAY = 0.6
MAX_DELAY = 3.0


def strip_pause_tags(text: str) -> str:
    return PAUSE_RE.sub("", text or "").strip()


def parse_pause_delay(attrs: str) -> float:
    match = _MS_RE.search(attrs or "")
    if not match:
        return DEFAULT_DELAY
    try:
        ms = int(match.group(1))
    except ValueError:
        return DEFAULT_DELAY
    if ms < 0:
        return DEFAULT_DELAY
    return min(ms / 1000.0, MAX_DELAY)
