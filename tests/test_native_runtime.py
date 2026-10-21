"""Offline checks for the Docker-free Windows backend launcher."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class NativeRuntimeTests(unittest.TestCase):
    def test_environment_from_installation_overrides_stale_host_transport(self):
        path = ROOT / "scripts/windows/native-service.py"
        spec = importlib.util.spec_from_file_location("native_service_fixture", path)
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ, {"LAY4FLOW_HOST_PROXY": "http://host.docker.internal:7890"}
            ),
        ):
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "LAY4FLOW_HOST_PROXY=http://127.0.0.1:49231\nOTHER_FIXTURE=fresh\n",
                encoding="utf-8",
            )
            service.load_env(env_file)
            self.assertEqual(os.environ["LAY4FLOW_HOST_PROXY"], "http://127.0.0.1:49231")
            self.assertEqual(os.environ["OTHER_FIXTURE"], "fresh")

    @unittest.skipUnless(sys.platform == "win32", "Windows launcher parser")
    def test_powershell_launchers_parse_without_system_pid_variables(self):
        code = r"""param([string]$RepoRoot)
$ErrorActionPreference = 'Stop'
foreach ($name in @('native-start.ps1','native-stop.ps1')) {
  $tokens=$null; $parseErrors=$null
  $ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot ('scripts/windows/' + $name)),[ref]$tokens,[ref]$parseErrors)
  if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
  $bad=$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.ParameterAst] -and $node.Name.VariablePath.UserPath -eq 'Pid'},$true)
  if ($bad.Count) { throw 'A parameter shadows system PID.' }
}
"""
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "check.ps1"
            script.write_text(code, encoding="utf-8-sig")
            result = subprocess.run(
                [
                    shutil.which("powershell.exe"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-RepoRoot",
                    str(ROOT),
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
