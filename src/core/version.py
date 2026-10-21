"""Single release version source shared by the API and release tooling."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

if not VERSION:
    raise RuntimeError("VERSION must not be empty")
