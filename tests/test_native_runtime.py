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
    @unittest.skipUnless(sys.platform == "win32", "Windows bundled Python diagnostics")
    def test_installer_keeps_python_traceback_and_accepts_successful_stderr(self):
        with tempfile.TemporaryDirectory(prefix="lay4flow-native-diagnostics-") as directory:
            fixture = Path(directory)
            diagnostic = fixture / "python diagnostic.py"
            diagnostic.write_text(
                "import sys\n"
                "print('ordinary Python progress', file=sys.stderr)\n"
                "if sys.argv[1] == 'fail':\n"
                "    raise RuntimeError('synthetic installer validation detail')\n",
                encoding="utf-8",
            )
            script = fixture / "check.ps1"
            script.write_text(
                r"""param([string]$RepoRoot, [string]$Fixture, [string]$Interpreter)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$LogPath = Join-Path $Fixture 'install.log'
$python = $Interpreter
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/native-install.ps1'), [ref]$tokens, [ref]$parseErrors)
foreach ($name in @('Log', 'Run-Python')) {
  $definition = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
  if (-not $definition) { throw "Missing installer function: $name" }
  Invoke-Expression $definition.Extent.Text
}
$diagnostic = Join-Path $Fixture 'python diagnostic.py'
Run-Python @($diagnostic, 'success')
$failure = ''
try { Run-Python @($diagnostic, 'fail') } catch { $failure = $_.Exception.Message }
if ($failure -notmatch 'Python exit code 1' -or $failure -notmatch 'Traceback' -or $failure -notmatch 'synthetic installer validation detail') {
  throw "Python failure lost its diagnostic: $failure"
}
$log = Get-Content -LiteralPath $LogPath -Raw -Encoding UTF8
foreach ($expected in @('ordinary Python progress', 'Bundled Python exit code: 0', 'Bundled Python exit code: 1', 'Traceback', 'synthetic installer validation detail')) {
  if ($log -notmatch [regex]::Escape($expected)) { throw "Python install log is missing: $expected" }
}
""",
                encoding="utf-8-sig",
            )
            result = subprocess.run(
                [
                    shutil.which("powershell.exe"),
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-RepoRoot",
                    str(ROOT),
                    "-Fixture",
                    str(fixture),
                    "-Interpreter",
                    sys.executable,
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
