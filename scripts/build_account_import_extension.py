"""Build a self-hosted lay4flow-remote account-login extension package."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = ROOT / "account_import_extension"
TEMPLATE_PATH = EXTENSION_ROOT / "manifest.template.json"
PLACEHOLDER = "__LAY4FLOW_MANAGEMENT_MATCH__"


def normalize_origin(value: str) -> str:
    """Return a canonical HTTP(S) origin suitable for a Chrome match pattern."""

    raw = str(value or "").strip()
    if (
        not raw
        or any(char.isspace() or ord(char) < 32 for char in raw)
        or any(char in raw for char in ("*", "\\", "?", "#", "%"))
    ):
        raise ValueError("origin must be an explicit HTTP or HTTPS origin")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin has an invalid port") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("origin must use http or https")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("origin must contain only a host and optional port")
    if parsed.netloc.endswith(":"):
        raise ValueError("origin port cannot be empty")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("origin cannot contain a path, query, or fragment")
    if any(char.isspace() for char in parsed.hostname) or "%" in parsed.hostname:
        raise ValueError("origin host is invalid")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("origin port must be between 1 and 65535")

    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def render_manifest(origin: str, template_path: Path = TEMPLATE_PATH) -> dict:
    """Render the static-host manifest used by one self-hosted deployment."""

    canonical = normalize_origin(origin)
    management_match = f"{canonical}/*"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    rendered = json.loads(
        json.dumps(template, ensure_ascii=False).replace(PLACEHOLDER, management_match)
    )

    hosts = rendered.get("host_permissions", [])
    matches = [entry.get("matches", []) for entry in rendered.get("content_scripts", [])]
    if management_match not in hosts or not any(management_match in group for group in matches):
        raise ValueError("manifest template does not expose the management origin")
    if "optional_host_permissions" in rendered or "scripting" in rendered.get("permissions", []):
        raise ValueError("static manifest must not request dynamic host or scripting permissions")
    return rendered


def _runtime_files() -> tuple[Path, ...]:
    # Explicit public files only; a stray local export must never enter a ZIP.
    required = {"background.js", "content.js", "flow_identity.js", "README.md"}
    files = [EXTENSION_ROOT / name for name in sorted(required)]
    missing = [path.name for path in files if not path.is_file() or path.is_symlink()]
    if missing:
        missing = ", ".join(missing)
        raise FileNotFoundError(f"extension source is missing: {missing}")
    files.extend(ROOT / name for name in ("LICENSE", "NOTICE"))
    return tuple(files)


def _static_background(source: str) -> str:
    """Remove optional-origin action flow from a static deployment build."""
    start = source.find("function originPattern(")
    end = source.find("function sendAdmin", start)
    if start < 0 or end < 0:
        raise ValueError("background source has no dynamic permission section")
    source = source[:start] + source[end:]
    source = source.replace("const STATIC_MANIFEST = false;", "const STATIC_MANIFEST = true;")
    source = source.replace(
        "if (!STATIC_MANIFEST) ensureManagePermission(tab).catch(() => {});",
        "void tab; // Permissions are fixed to this deployment's origin.",
    )
    return source


def _write_package(directory: Path, manifest: dict) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    for source in _runtime_files():
        if source.name == "background.js":
            (directory / source.name).write_text(
                _static_background(source.read_text(encoding="utf-8")),
                encoding="utf-8",
                newline="\n",
            )
        else:
            shutil.copy2(source, directory / source.name)


def build_extension(origin: str, output: Path) -> dict:
    """Build a directory or .zip package and return its rendered manifest."""

    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    manifest = render_manifest(origin)
    output.parent.mkdir(parents=True, exist_ok=True)

    if output.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="lay4flow-extension-") as temp_dir:
            staging = Path(temp_dir) / "extension"
            _write_package(staging, manifest)
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(staging.iterdir()):
                    archive.write(path, path.name)
    else:
        _write_package(output, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="The exact self-hosted HTTP(S) origin")
    parser.add_argument("--output", required=True, type=Path, help="Output directory or .zip file")
    args = parser.parse_args()
    manifest = build_extension(args.origin, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "management_match": manifest["content_scripts"][0]["matches"][0],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
