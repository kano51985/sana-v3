"""Hash-only backup manifests for immutable migration source inspection."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sana.app.migration.service import BackupFile


def hash_file(path: Path) -> BackupFile:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return BackupFile(str(path.resolve()), path.stat().st_size, digest.hexdigest())


def manifest_hash(files: tuple[BackupFile, ...]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value.path):
        digest.update(item.path.encode("utf-8"))
        digest.update(str(item.size).encode("ascii"))
        digest.update(item.sha256.encode("ascii"))
    return digest.hexdigest()
