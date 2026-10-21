"""Build versioned public assets and their SHA-256 list from one source snapshot."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from package_release import ROOT, package_release
from package_windows import package_windows


def build_release(root: Path, output: Path, desktop_runtime: Path | None = None) -> dict:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("VERSION must be a stable semantic version")
    source = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, encoding="utf-8"
    ).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root))
    output.mkdir(parents=True, exist_ok=True)
    runtime = output / f"lay4flow-remote-{version}-runtime.tar.gz"
    windows = output / f"lay4flow-remote-{version}-windows-x64.zip"
    if any((output / name).exists() for name in (runtime.name, windows.name, "SHA256SUMS")):
        raise FileExistsError("Release assets already exist; use a fresh output directory")
    manifest = package_release(root, runtime)
    resolved_desktop = desktop_runtime
    if resolved_desktop is None:
        candidate = root / "desktop" / "runtime" / "windows-x64"
        if (candidate / "Lay4Flow.Desktop.exe").is_file():
            resolved_desktop = candidate
    package_windows(root, windows, runtime_archive=runtime, desktop_runtime=resolved_desktop)
    assets = []
    for path in (runtime, windows):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assets.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest})
    (output / "SHA256SUMS").write_text(
        "".join(f"{asset['sha256']}  {asset['name']}\n" for asset in assets), encoding="ascii"
    )
    changelog = (root / "docs/CHANGELOG.md").read_text(encoding="utf-8")
    marker = f"## {version} — "
    if marker not in changelog:
        raise ValueError("The release version must have changelog notes")
    notes = changelog[changelog.index(marker) :].split("\n## ", 1)[0].rstrip() + "\n"
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    evidence = {
        "project": "lay4flow-remote",
        "version": version,
        "source_commit": source,
        "working_tree_dirty": dirty,
        "assets": assets,
        "runtime_files": manifest["files"],
        "windows_signing": "unsigned_script_bundle",
    }
    (output / "build-manifest.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--desktop-runtime", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build_release(ROOT, args.output.resolve(), args.desktop_runtime),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
