"""Build a runtime archive from a public-file allowlist, never from runtime state."""

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = (
    "main.py",
    "VERSION",
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
    "docs/API.md",
    "docs/VIDS_API.md",
    "docs/DOWNSTREAM_COMPAT.md",
    "docs/FLOW_PUBLIC_MODELS.md",
    "docs/FRONTEND.md",
    "docs/REMOTE_BROWSER_FINGERPRINT.md",
    "docs/RELEASE_PACKAGE.md",
    "docs/CHANGELOG.md",
    "docs/images/qq-group.jpg",
    "docs/screenshots/README.md",
    "docs/screenshots/login.webp",
    "docs/screenshots/dashboard.webp",
    "docs/screenshots/studio.webp",
    "docs/screenshots/image-editor.webp",
    "docs/screenshots/vids-studio.webp",
    "docs/screenshots/accounts.webp",
    "docs/screenshots/session-transfer.webp",
    "docs/screenshots/proxy-pool.webp",
    "docs/screenshots/api-guide.webp",
    "docs/screenshots/settings.webp",
    "docs/screenshots/mobile.webp",
    "docs/screenshots/updates.webp",
    "scripts/smoke_package.py",
    "scripts/build_account_import_extension.py",
    "account_import_extension/manifest.json",
    "account_import_extension/manifest.template.json",
    "account_import_extension/background.js",
    "account_import_extension/content.js",
    "account_import_extension/flow_identity.js",
    "account_import_extension/README.md",
    "scripts/capture_readme.mjs",
    "scripts/update.ps1",
    "scripts/update.sh",
    "scripts/update_runtime.py",
    "docs/DOCKER_UPDATE.md",
    "docs/WINDOWS_INSTALL.md",
)
TREE_SUFFIXES = {
    "src": {".py"},
    "docker": {".sh"},
    "static": {
        ".html",
        ".js",
        ".mjs",
        ".css",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".txt",
    },
}
EXCLUDED_DIRS = {
    "__pycache__",
    ".git",
    "node_modules",
    "data",
    "tmp",
    "output",
    "evidence",
    "sponsors",
}


def runtime_files(root: Path):
    """Yield the sorted release inventory, rejecting missing required public files."""
    root = Path(root).resolve()
    files = set()
    for name in TOP_LEVEL:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Required release file is missing: {name}")
        files.add(path)
    for directory, suffixes in TREE_SUFFIXES.items():
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in suffixes:
                continue
            if any(part in EXCLUDED_DIRS or part.startswith(".") for part in relative.parts):
                continue
            if any(marker in path.name.lower() for marker in (".private", ".local.", ".session.")):
                continue
            files.add(path)
    yield from sorted(files, key=lambda path: path.relative_to(root).as_posix())


def package_release(root: Path, output: Path) -> dict:
    root, output = Path(root).resolve(), Path(output).resolve()
    files = list(runtime_files(root))
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in files:
            info = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            with path.open("rb") as source:
                archive.addfile(info, source)
    with output.open("rb") as packaged:
        sha256 = hashlib.file_digest(packaged, "sha256").hexdigest()
    return {
        "project": "lay4flow-remote",
        "version": version,
        "files": [path.relative_to(root).as_posix() for path in files],
        "sha256": sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(package_release(ROOT, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
