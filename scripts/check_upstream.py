"""Compare sanitized Flow GetModels snapshots and report adapter coverage, offline."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_snapshot(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not all(
        isinstance(data.get(k), list) for k in ("video", "image")
    ):
        raise ValueError("Expected a sanitized GetModels snapshot with video/image lists")
    for kind in ("video", "image"):
        for group in data[kind]:
            if not isinstance(group, dict):
                raise ValueError(f"Invalid {kind} group")
            entries = group.get("models" if kind == "video" else "keys")
            if not isinstance(entries, list):
                raise ValueError(f"Invalid {kind} model list")
            for item in entries:
                if kind == "video" and not isinstance(item, dict):
                    raise ValueError("Invalid video model entry")
                key = item.get("key") if kind == "video" and isinstance(item, dict) else item
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"Invalid {kind} model key")
    return data


def snapshot_keys(snapshot: dict) -> dict[str, set[str]]:
    return {
        "video": {m["key"] for group in snapshot["video"] for m in group["models"]},
        "image": {key for group in snapshot["image"] for key in group["keys"]},
    }


def compare_snapshots(previous: dict, current: dict, catalog: dict) -> dict:
    before, after = snapshot_keys(previous), snapshot_keys(current)
    used: dict[str, set[str]] = {"video": set(), "image": set()}
    for cfg in catalog.values():
        if cfg["type"] == "video":
            used["video"].update(cfg[k] for k in ("model_key", "reference_model_key") if cfg.get(k))
            if cfg.get("upsample"):
                used["video"].add(cfg["upsample"]["model_key"])
        else:
            used["image"].add(cfg["model_name"])
    report = {
        kind: {
            "added": sorted(after[kind] - before[kind]),
            "removed": sorted(before[kind] - after[kind]),
            "newly_missing_used_keys": sorted((before[kind] - after[kind]) & used[kind]),
            "used_not_advertised": sorted(used[kind] - after[kind]),
            "advertised_not_used": sorted(after[kind] - used[kind]),
        }
        for kind in ("video", "image")
    }
    old_video = {m["key"]: m for group in previous["video"] for m in group["models"]}
    new_video = {m["key"]: m for group in current["video"] for m in group["models"]}
    report["video"]["changed_definitions"] = {
        key: {"before": old_video[key], "after": new_video[key]}
        for key in sorted(old_video.keys() & new_video.keys())
        if old_video[key] != new_video[key]
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=Path, help="New sanitized GetModels JSON")
    parser.add_argument(
        "--previous", type=Path, default=ROOT / "docs/evidence/flow-live-models-20260925.json"
    )
    parser.add_argument("--fail-on-removal", action="store_true")
    args = parser.parse_args()
    from src.core.model_catalog import MODEL_CONFIG

    try:
        result = compare_snapshots(
            read_snapshot(args.previous), read_snapshot(args.current), MODEL_CONFIG
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Missing previously unadvertised legacy keys remain informational: a model
    # list is evidence, not proof that an old endpoint or model stopped working.
    return int(
        args.fail_on_removal and any(row["newly_missing_used_keys"] for row in result.values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
