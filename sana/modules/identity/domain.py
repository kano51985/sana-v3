"""Authenticated identity carried through a request."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    issuer: str
    subject: str

    def __post_init__(self) -> None:
        if not self.issuer.strip():
            raise ValueError("issuer cannot be empty")
        if not self.subject.strip():
            raise ValueError("subject cannot be empty")
