"""Local non-secret Vids bindings and separately protected encryption keys."""

import json
import os
import stat
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

MAX_BINDINGS_BYTES = 1024 * 1024


def read_bindings(path):
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(MAX_BINDINGS_BYTES + 1)
    except FileNotFoundError:
        return []
    value = json.loads(data)
    if (
        len(data) > MAX_BINDINGS_BYTES
        or not isinstance(value, dict)
        or set(value) != {"version", "accounts"}
        or value["version"] != 1
        or not isinstance(value["accounts"], list)
    ):
        raise ValueError("Invalid Vids binding registry")
    return value["accounts"]


def write_bindings(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bindings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(
                json.dumps({"version": 1, "accounts": rows}, separators=(",", ":")).encode()
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def session_key_file(path, *, create=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Invalid Vids session key file")
    path = path.resolve()
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL preserves a previously created key if another process wins the race.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(Fernet.generate_key())
            handle.flush()
            os.fsync(handle.fileno())
    if path.is_symlink() or not path.is_file():
        raise ValueError("Invalid Vids session key file")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Vids session key file must be private")
    value = path.read_bytes()
    if len(value) != 44:
        raise ValueError("Invalid Vids session key file")
    Fernet(value)
    return value
