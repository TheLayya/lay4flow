"""Persistent tombstones preventing deleted static Vids bindings from returning."""

import os
import tempfile
from pathlib import Path


def marker_dir(registry_path) -> Path:
    return Path(registry_path or "data/vids/bindings.json").parent / "deleted-accounts"


def is_deleted(registry_path, token_id: int) -> bool:
    return (marker_dir(registry_path) / str(token_id)).is_file()


def mark_deleted(registry_path, token_id: int) -> None:
    directory = marker_dir(registry_path)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".deleted-", dir=directory)
    try:
        os.close(fd)
        os.replace(temporary, directory / str(token_id))
    finally:
        Path(temporary).unlink(missing_ok=True)


def clear_deleted(registry_path, token_id: int) -> None:
    (marker_dir(registry_path) / str(token_id)).unlink(missing_ok=True)
