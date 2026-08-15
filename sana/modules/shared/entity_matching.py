"""Deterministic matching of configured product entities inside model labels."""

from __future__ import annotations

from collections.abc import Iterable
import re


def match_configured_entity(
    value: str,
    configured_entities: Iterable[str],
) -> str | None:
    """Return the most specific configured entity at an ASCII word boundary."""

    normalized = " ".join(value.casefold().split())
    matches = []
    for raw in configured_entities:
        configured = " ".join(raw.casefold().split())
        if not configured:
            continue
        pattern = rf"(?<![0-9a-z]){re.escape(configured)}(?![0-9a-z])"
        if re.search(pattern, normalized):
            matches.append(configured)
    return max(matches, key=lambda item: (len(item), item), default=None)


__all__ = ["match_configured_entity"]
