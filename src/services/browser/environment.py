"""Small, non-sensitive browser environment snapshots for profile reuse."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EnvironmentSnapshot:
    user_agent: str
    language: str
    languages: tuple[str, ...]
    timezone: str
    platform: str
    viewport_width: int
    viewport_height: int

    @classmethod
    def from_probe(cls, value: dict[str, Any]) -> "EnvironmentSnapshot":
        languages = value.get("languages", ())
        if not isinstance(languages, (list, tuple)):
            languages = ()
        snapshot = cls(
            user_agent=str(value.get("userAgent", "")),
            language=str(value.get("language", "")),
            languages=tuple(str(item) for item in languages),
            timezone=str(value.get("timezone", "")),
            platform=str(value.get("platform", "")),
            viewport_width=int(value.get("width", 0)),
            viewport_height=int(value.get("height", 0)),
        )
        if not snapshot.user_agent or not snapshot.language or not snapshot.timezone:
            raise ValueError("browser environment snapshot is incomplete")
        if min(snapshot.viewport_width, snapshot.viewport_height) <= 0:
            raise ValueError("browser viewport is invalid")
        return snapshot

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> "EnvironmentSnapshot":
        data = json.loads(value)
        data["languages"] = tuple(data.get("languages", ()))
        return cls(**data)


class EnvironmentSnapshotStore:
    def __init__(self, profile_dir: str | Path):
        self.path = Path(profile_dir) / "environment_snapshot.json"

    def check_or_record(self, snapshot: EnvironmentSnapshot) -> bool:
        if not self.path.exists():
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(snapshot.to_json(), encoding="utf-8")
            temporary.replace(self.path)
            return True
        stored = EnvironmentSnapshot.from_json(self.path.read_text(encoding="utf-8"))
        return stored == snapshot
