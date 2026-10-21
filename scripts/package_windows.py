"""Package the public runtime and native Docker Desktop one-click installer."""

import argparse
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

from package_release import ROOT, package_release


def package_windows(
    root: Path,
    output: Path,
    runtime_archive: Path | None = None,
    desktop_runtime: Path | None = None,
) -> dict:
    root, output = Path(root).resolve(), Path(output).resolve()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    bundle_name = f"lay4flow-remote-{version}-windows"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lay4flow-windows-package-") as directory:
        runtime_name = f"lay4flow-remote-{version}-runtime.tar.gz"
        runtime_path = (
            Path(runtime_archive).resolve() if runtime_archive else Path(directory) / runtime_name
        )
        runtime = (
            package_release(root, runtime_path)
            if runtime_archive is None
            else {
                "sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
                "files": [],
            }
        )
        runtime_name = runtime_path.name
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted((root / "scripts/windows").glob("*")):
                if path.is_file() and path.suffix in {".cmd", ".ps1", ".ico"}:
                    archive.write(path, f"{bundle_name}/windows/{path.name}")
            archive.write(root / "scripts/windows/install.cmd", f"{bundle_name}/install.cmd")
            archive.write(root / "scripts/windows/install.ps1", f"{bundle_name}/install.ps1")
            for name in ("start", "stop", "uninstall", "update"):
                launcher = (
                    "@echo off\r\nsetlocal\r\n"
                    f'powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\\{name}.ps1" %*\r\n'
                    "if errorlevel 1 pause\r\nendlocal\r\n"
                )
                archive.writestr(f"{bundle_name}/{name}.cmd", launcher.encode("ascii"))
            if desktop_runtime is not None:
                desktop_runtime = Path(desktop_runtime).resolve()
                exe = desktop_runtime / "Lay4Flow.Desktop.exe"
                if not exe.is_file():
                    raise FileNotFoundError(f"Desktop runtime is missing {exe}")
                for path in sorted(desktop_runtime.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(desktop_runtime)
                        allowed_json = path.parent == desktop_runtime and path.name in {
                            "Lay4Flow.Desktop.deps.json",
                            "Lay4Flow.Desktop.runtimeconfig.json",
                        }
                        if (
                            path.is_symlink()
                            or path.suffix.lower() not in {".dll", ".exe", ".pdb", ".xml", ".json"}
                            or path.suffix.lower() == ".json"
                            and not allowed_json
                            or any(
                                part.lower() in {"data", "tmp", "profiles", "output", "cookies"}
                                for part in relative.parts
                            )
                        ):
                            raise ValueError(f"Desktop runtime contains private state: {relative}")
                        archive.write(
                            path, f"{bundle_name}/desktop/windows-x64/{relative.as_posix()}"
                        )
            archive.write(root / "docs/WINDOWS_INSTALL.md", f"{bundle_name}/README.md")
            archive.write(root / "LICENSE", f"{bundle_name}/LICENSE")
            archive.write(root / "NOTICE", f"{bundle_name}/NOTICE")
            archive.write(runtime_path, f"{bundle_name}/runtime/{runtime_name}")
            archive.writestr(
                f"{bundle_name}/runtime/SHA256SUMS",
                f"{runtime['sha256']}  {runtime_name}\n".encode("ascii"),
            )
    return {
        "project": "lay4flow-remote",
        "version": version,
        "kind": "windows-desktop-docker-installer"
        if desktop_runtime
        else "windows-docker-installer",
        "desktop": bool(desktop_runtime),
        "requires": ["Windows 10/11 x64", "Docker Desktop with Linux containers"],
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "runtime_sha256": runtime["sha256"],
        "runtime_files": runtime["files"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--desktop-runtime", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            package_windows(ROOT, args.output, desktop_runtime=args.desktop_runtime),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
