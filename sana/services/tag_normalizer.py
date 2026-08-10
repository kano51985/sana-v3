import re


_PAUSE_TAG_RE = re.compile(r"<\s*pause\b([^>]*)>", re.IGNORECASE)


def normalize_pause_tags(text: str) -> str:
    if not text:
        return text or ""

    def replace(match):
        attrs = re.sub(r"\s+", " ", match.group(1)).strip()
        attrs = re.sub(r"\s*/\s*$", "", attrs)
        if attrs:
            return f"<pause {attrs}>"
        return "<pause>"

    return _PAUSE_TAG_RE.sub(replace, text)


def normalize_tags(text: str) -> str:
    return normalize_pause_tags(text)
