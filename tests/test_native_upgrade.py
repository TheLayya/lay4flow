"""Synthetic Windows ownership and migration regressions; never use installed accounts."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows installer integration")
class NativeUpgradeTests(unittest.TestCase):
    def run_powershell(self, code, *, timeout=30):
        with tempfile.TemporaryDirectory(prefix="lay4flow-upgrade-fixture-") as directory:
            fixture = Path(directory)
            script = fixture / "check.ps1"
            script.write_text(
                "param([string]$RepoRoot,[string]$Fixture)\n"
                "Set-StrictMode -Version Latest\n$ErrorActionPreference='Stop'\n" + code,
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
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_orphan_native_identity_is_proven_from_exact_arguments_not_pid_cache(self):
        self.run_powershell(
            r"""
. (Join-Path $RepoRoot 'scripts/windows/native-process.ps1')
$InstallDir=Join-Path $Fixture 'installed space'
$python=Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64\python.exe'
$service=Join-Path $InstallDir '.lay4flow-windows\native-service.py'
$stop=Join-Path $InstallDir 'data\native-service.stop'
$log=Join-Path $InstallDir 'data\native-service.log'
$command='"'+$python+'" "'+$service+'" --root "'+$InstallDir+'" --port 38003 --stop-file "'+$stop+'" --log-file "'+$log+'"'
$script:candidates=@(
  [pscustomobject]@{ProcessId=101;ExecutablePath=$python;CommandLine=$command},
  [pscustomobject]@{ProcessId=102;ExecutablePath=$python;CommandLine=$command.Replace('--root "'+$InstallDir+'"','--root "'+$InstallDir+'-other"')},
  [pscustomobject]@{ProcessId=103;ExecutablePath=$python+'-other';CommandLine=$command},
  [pscustomobject]@{ProcessId=104;ExecutablePath=$python;CommandLine=$command+' --root "'+$InstallDir+'"'},
  [pscustomobject]@{ProcessId=105;ExecutablePath=$python;CommandLine=$command.Replace($service,$service+'-other')},
  [pscustomobject]@{ProcessId=106;ExecutablePath=$python;CommandLine=$command.Replace($stop,$stop+'-other')}
)
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction) return $script:candidates }
function Get-Process { param($Id,$ErrorAction) return [pscustomobject]@{Id=$Id;HasExited=$false;StartTime=[datetime]'2026-01-02T00:00:00Z'} }
$owned=@(Get-NativeOwnedProcesses $InstallDir)
if($owned.Count -ne 1 -or $owned[0].Process.Id -ne 101 -or $owned[0].Port -ne 38003){throw 'Ownership boundary failed'}
if(Test-Path -LiteralPath (Join-Path $InstallDir 'data/native-service.json')){throw 'Test unexpectedly depends on saved PID state'}
"""
        )

    def test_installer_port_update_preserves_all_other_private_env_settings(self):
        self.run_powershell(
            r"""
$envPath=Join-Path $Fixture '.env'
[IO.File]::WriteAllText($envPath,"# synthetic settings`r`nLAY4FLOW_REMOTE_PORT=38003`r`nCOMPOSE_PROJECT_NAME=owned-fixture`r`nLAY4FLOW_HOST_PROXY=socks5://synthetic.invalid:1080`r`nUNRELATED=keep-value`r`n",[Text.UTF8Encoding]::new($false))
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/native-install.ps1'),[ref]$tokens,[ref]$errors)
$definition=$ast.Find({param($node)$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Save-NativePort'},$false)
Invoke-Expression $definition.Extent.Text
Save-NativePort 38004
$value=[IO.File]::ReadAllText($envPath)
foreach($expected in @('LAY4FLOW_REMOTE_PORT=38004','COMPOSE_PROJECT_NAME=owned-fixture','LAY4FLOW_HOST_PROXY=socks5://synthetic.invalid:1080','UNRELATED=keep-value')){if(-not $value.Contains($expected)){throw 'An unrelated setting changed'}}
if($value -match '38003'){throw 'Previous port was retained'}
if(@(Get-ChildItem -LiteralPath $Fixture -Filter '*.port-*').Count){throw 'Temporary settings file was not cleaned'}
"""
        )

    def test_native_install_without_docker_does_not_treat_compose_file_as_running_backend(self):
        self.run_powershell(
            r"""
$InstallDir=Join-Path $Fixture 'installed'
New-Item -ItemType Directory -Path $InstallDir | Out-Null
[IO.File]::WriteAllText((Join-Path $InstallDir 'docker-compose.yml'),'services: {}')
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $RepoRoot 'scripts/windows/native-install.ps1'),[ref]$tokens,[ref]$errors)
$definition=$ast.Find({param($node)$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-OwnedLegacyContainers'},$false)
Invoke-Expression $definition.Extent.Text
function Get-Command { param($Name,$ErrorAction) return $null }
function Check-Port { param($Value) return $false }
if(@(Get-OwnedLegacyContainers).Count){throw 'A native bundle compose file was mistaken for an existing container'}
"""
        )


if __name__ == "__main__":
    unittest.main()
