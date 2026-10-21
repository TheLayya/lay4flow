"""Build the Windows backend runtime shipped inside the desktop installer.

The generated directory is intentionally outside the source tree.  It contains
the official Python embeddable distribution and wheels installed for Windows;
the application source remains in the normal release archive.  This keeps the
repository small while making the installer independent of Docker and of a
user-installed Python.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.12.10"
PYTHON_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
)
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"


def _download(url: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def _unpack_python(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)
    pth = next(destination.glob("python*._pth"), None)
    if pth is None:
        raise RuntimeError("Python embeddable archive did not contain a _pth file")
    lines = [line.strip() for line in pth.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line and line != "import site"]
    lines.extend(["Lib/site-packages", "import site"])
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _install_dependencies(python: Path, destination: Path, requirements: Path) -> None:
    site = destination / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    # Wheels are resolved by the build machine for win_amd64 and copied as a
    # flat site-packages tree. No package manager is needed on the user's PC.
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-compile",
        "--only-binary=:all:",
        "--target",
        str(site),
        "-r",
        str(requirements),
    ]
    subprocess.run(command, check=True)


def _copy_cloak_binary(destination: Path, source: Path | None) -> Path | None:
    if source is None:
        return None
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    # Chromium is a directory, not just chrome.exe: DLLs, ICU data, pak files,
    # crashpad and the profile helper are all loaded beside the executable.
    # Copy the complete verified cache directory to keep the native package
    # offline and avoid the first-run download which Docker previously hid.
    target = destination / "cloakbrowser"
    shutil.copytree(source.parent, target, dirs_exist_ok=True)
    return target / source.name


def build(
    output: Path,
    *,
    python_exe: Path,
    requirements: Path,
    cloak_binary: Path | None,
    python_archive: Path | None = None,
) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Choose a fresh native runtime output directory: {output}")
    output.mkdir(parents=True)
    cache = output.parent / ".native-runtime-cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if python_archive is not None:
        shutil.copy2(Path(python_archive).resolve(), archive)
    if not archive.exists():
        _download(PYTHON_URL, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != PYTHON_SHA256:
        raise ValueError("Python embeddable archive checksum verification failed")
    _unpack_python(archive, output)
    _install_dependencies(python_exe.resolve(), output, requirements.resolve())
    binary = _copy_cloak_binary(output, cloak_binary)
    manifest = {
        "runtime": "windows-x64-native",
        "python": PYTHON_VERSION,
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        "cloak_binary": binary.relative_to(output).as_posix() if binary else None,
        "files": sorted(path.relative_to(output).as_posix() for path in output.rglob("*")),
    }
    (output / "runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--python-exe", type=Path, default=Path(sys.executable))
    parser.add_argument("--requirements", type=Path, default=Path("requirements.txt"))
    parser.add_argument("--cloak-binary", type=Path)
    parser.add_argument("--python-archive", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.output,
                python_exe=args.python_exe,
                requirements=args.requirements,
                cloak_binary=args.cloak_binary,
                python_archive=args.python_archive,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
