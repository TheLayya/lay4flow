"""Offline archive and deployment-file operations used by the host updater.

Runs with Python from the existing Docker image; it never talks to Docker or a network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath

PUBLIC_ROOTS = {"src", "static", "docker", "docs", "scripts"}
EXTENSION_FILES = {
    "account_import_extension/manifest.json",
    "account_import_extension/manifest.template.json",
    "account_import_extension/background.js",
    "account_import_extension/content.js",
    "account_import_extension/flow_identity.js",
    "account_import_extension/README.md",
}
PUBLIC_FILES = {
    "VERSION",
    "main.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "NOTICE",
    "SECURITY.md",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    "config/setting_example.toml",
}
PRIVATE_PARTS = {
    "data",
    "tmp",
    "output",
    "outputs",
    ".git",
    ".lay4flow-updates",
    "node_modules",
    "__pycache__",
}


def filesystem_path(path: Path) -> Path:
    """Keep nested Profile backups within Windows' extended-length namespace."""
    if os.name != "nt":
        return path
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def public_path(name: str) -> bool:
    if "\\" in name or ":" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        return False
    if any(part.lower() in PRIVATE_PARTS for part in path.parts):
        return False
    if name in PUBLIC_FILES:
        return True
    if name in EXTENSION_FILES:
        return True
    if not path.parts or path.parts[0] not in PUBLIC_ROOTS:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    lower = name.lower()
    return not any(
        marker in lower
        for marker in (".private", ".local.", ".session.", ".pem", ".key", ".db", ".sqlite", ".env")
    )


def prepare(archive: Path, checksums: Path, candidate: Path, version: str) -> dict:
    name = f"lay4flow-remote-{version}-runtime.tar.gz"
    matches = [
        match.group(1).lower()
        for line in checksums.read_text(encoding="utf-8-sig").splitlines()
        if (match := re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip()))
        and match.group(2) == name
    ]
    if len(matches) != 1:
        raise ValueError(f"SHA256SUMS must contain exactly one entry for {name}")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != matches[0]:
        raise ValueError("Runtime archive SHA-256 mismatch; current deployment was preserved")
    if candidate.exists():
        raise ValueError("Candidate directory already exists")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = set()
        size = 0
        for member in members:
            if not member.isfile() or not public_path(member.name) or member.name in names:
                raise ValueError(f"Unexpected runtime archive entry: {member.name}")
            names.add(member.name)
            size += member.size
        if size > 512 * 1024**2:
            raise ValueError("Runtime archive exceeds the 512 MiB unpacked limit")
        required = {
            "VERSION",
            "Dockerfile",
            "docker-compose.yml",
            "main.py",
            "requirements.txt",
            "src/main.py",
        }
        if not required.issubset(names):
            raise ValueError("Runtime archive is missing required application files")
        candidate.mkdir(parents=True)
        for member in members:
            destination = candidate / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"Missing archive content: {member.name}")
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            destination.chmod(0o755 if member.name.endswith(".sh") else 0o644)
    if (candidate / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("Runtime archive VERSION differs from the selected release")
    inventory = {"version": version, "sha256": actual, "files": sorted(names)}
    (candidate.parent / "candidate.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )
    return inventory


def public_files(root: Path):
    for name in sorted(PUBLIC_FILES | EXTENSION_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            yield path, name
    for folder in sorted(PUBLIC_ROOTS):
        base = root / folder
        if base.is_symlink() or not base.is_dir():
            continue
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if name.lower() not in PRIVATE_PARTS
                and not name.startswith(".")
                and not (Path(current) / name).is_symlink()
            ]
            for filename in sorted(files):
                path = Path(current) / filename
                name = path.relative_to(root).as_posix()
                if not path.is_symlink() and public_path(name):
                    yield path, name


def checked_target(root: Path, name: str) -> Path:
    if not public_path(name):
        raise ValueError(f"Unexpected deployment path: {name}")
    target = root / name
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Deployment path uses a symlink: {name}")
        current = current.parent
    return target


def snapshot(
    root: Path,
    backup: Path,
    candidate: Path,
    image: str,
    image_id: str = "",
    health_version: str | None = None,
) -> dict:
    root, backup, candidate = map(filesystem_path, (root, backup, candidate))
    if backup.exists():
        raise ValueError("Backup directory already exists")
    backup.mkdir(parents=True)
    backup.chmod(0o700)
    files = []
    for path, name in public_files(root):
        target = backup / "runtime" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files.append(name)
    for name in ("data", "tmp", "config/setting.toml", ".env"):
        source = root / name
        target = backup / "persistent" / name
        if source.is_symlink():
            raise ValueError(f"Persistent mount must use a real path: {name}")
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    candidate_files = [name for _, name in public_files(candidate)]
    metadata = {
        "image": image,
        "image_id": image_id or image,
        "version": (root / "VERSION").read_text(encoding="utf-8").strip()
        if (root / "VERSION").exists()
        else "",
        "old_files": sorted(files),
        "added_files": sorted(set(candidate_files) - set(files)),
    }
    metadata["health_version"] = metadata["version"] if health_version is None else health_version
    (backup / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def set_image(root: Path, image: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", image):
        raise ValueError("Invalid Docker image selection")
    path = root / ".env"
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    lines = [
        line
        for line in original.splitlines()
        if not re.match(r"\s*LAY4FLOW_REMOTE_IMAGE\s*=", line)
    ]
    lines.append(f"LAY4FLOW_REMOTE_IMAGE={image}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = root / ".lay4flow-updates"
    state.mkdir(parents=True, exist_ok=True)
    (state / "image-override.json").write_text(
        json.dumps({"services": {"app": {"image": image}}}) + "\n", encoding="utf-8"
    )


def apply(root: Path, candidate: Path) -> None:
    for source, name in public_files(candidate):
        target = checked_target(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def restore(root: Path, backup: Path, *, restore_data: bool = False) -> dict:
    root, backup = map(filesystem_path, (root, backup))
    metadata = json.loads((backup / "metadata.json").read_text(encoding="utf-8"))
    for name in metadata["added_files"]:
        if public_path(name):
            if name in {"scripts/update.ps1", "scripts/update.sh", "scripts/update_runtime.py"}:
                continue
            checked_target(root, name).unlink(missing_ok=True)
    apply(root, backup / "runtime")
    if metadata.get("image"):
        set_image(root, metadata["image"])
    if restore_data:
        for name in ("data", "tmp"):
            saved = backup / "persistent" / name
            current = root / name
            if saved.exists():
                failed = backup / ("failed-" + name)
                if current.exists():
                    if failed.exists():
                        raise ValueError(f"Preserved failed data already exists: {failed}")
                    current.rename(failed)
                shutil.copytree(saved, current, symlinks=True)
    # .env/config are never overwritten by an update; keep all current local values.
    return metadata


def assert_idle(root: Path) -> dict:
    import sqlite3

    active = 0
    studio = root / "data/studio-jobs.json"
    if studio.exists():
        jobs = json.loads(studio.read_text(encoding="utf-8"))
        active += sum(
            item.get("status") in {"queued", "generating"}
            for job in jobs
            for item in job.get("items", [])
        )
    database = root / "data/flow.db"
    if database.exists():
        with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "video_jobs" in tables:
                active += connection.execute(
                    "SELECT COUNT(*) FROM video_jobs WHERE status IN ('queued','in_progress')"
                ).fetchone()[0]
    if active:
        raise ValueError(
            f"{active} queued/running generation jobs remain; retry the update after they finish"
        )
    return {"active_jobs": active}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "snapshot", "apply", "restore", "idle", "set-image")
    )
    parser.add_argument("--root", type=Path, default=Path("/deploy"))
    parser.add_argument("--stage", type=Path, default=Path("/update"))
    parser.add_argument("--version")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--image", default="")
    parser.add_argument("--image-id", default="")
    parser.add_argument("--health-version")
    parser.add_argument("--restore-data", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(
            args.stage / "runtime.tar.gz",
            args.stage / "SHA256SUMS",
            args.stage / "candidate",
            args.version,
        )
    elif args.action == "snapshot":
        result = snapshot(
            args.root,
            args.backup,
            args.stage / "candidate",
            args.image,
            args.image_id,
            args.health_version,
        )
    elif args.action == "apply":
        result = apply(args.root, args.stage / "candidate")
    elif args.action == "restore":
        result = restore(args.root, args.backup, restore_data=args.restore_data)
    elif args.action == "set-image":
        result = set_image(args.root, args.image)
    else:
        result = assert_idle(args.root)
    summary = dict(result or {"status": "passed"})
    for field in ("files", "old_files", "added_files"):
        if field in summary:
            summary[field + "_count"] = len(summary.pop(field))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
