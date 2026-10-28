"""Run native install transactions with synthetic files and mocked Docker ownership."""

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows native migration integration")
class NativeMigrationTests(unittest.TestCase):
    def run_migration(self, mode):
        with tempfile.TemporaryDirectory(prefix="lay4flow-migration-test-") as temporary:
            fixture = Path(temporary)
            bundle = fixture / "bundle"
            installed = fixture / "installed"
            for target in (
                bundle / "runtime",
                bundle / "windows",
                bundle / "backend/windows-x64",
                bundle / "desktop/windows-x64",
                installed / "data",
                installed / "tmp",
                installed / "config",
                installed / ".lay4flow-windows/backend/windows-x64",
            ):
                target.mkdir(parents=True)
            # Native runtime leftovers must not suppress Docker migration.
            for target in (
                bundle / "backend/windows-x64/python.exe",
                installed / ".lay4flow-windows/backend/windows-x64/python.exe",
                bundle / "desktop/windows-x64/Lay4Flow.Desktop.exe",
            ):
                target.write_text("synthetic public runtime", encoding="ascii")
            (installed / "VERSION").write_text("1.5.4\n", encoding="ascii")
            (installed / "docker-compose.yml").write_text("services: {}\n", encoding="ascii")
            (installed / "config/setting.toml").write_text("synthetic='keep'\n", encoding="ascii")
            (installed / "data/account.txt").write_text("synthetic-account-keep", encoding="ascii")
            (installed / "tmp/media.txt").write_text("synthetic-media-keep", encoding="ascii")
            for name in ("native-process.ps1", "native-stop.ps1"):
                shutil.copy2(ROOT / "scripts/windows" / name, bundle / "windows" / name)
            (bundle / "windows/network.ps1").write_text(
                "param($InstallDir,[switch]$Native)\n", encoding="ascii"
            )
            (bundle / "windows/start.ps1").write_text(
                "param($InstallDir,$Port,[switch]$NoBrowser,[switch]$AlreadyStarted,[switch]$AllowMaintenance)\n",
                encoding="ascii",
            )
            (bundle / "windows/native-service.py").write_text("# fixture\n", encoding="ascii")
            (bundle / "windows/native-start.ps1").write_text(
                r"""param($InstallDir,$Port,[switch]$NoBrowser,[switch]$AllowMaintenance)
$global:events.Add('native-start')
if ($global:mode -ne 'foreign' -and $global:ownedRunning) { throw 'Docker was not stopped before native launch' }
if ($global:mode -eq 'fail') { throw 'synthetic native launch failure' }
$global:startedPort=[int]$Port
""",
                encoding="ascii",
            )
            runtime = bundle / "runtime/lay4flow-remote-1.5.5-runtime.tar.gz"
            contents = {
                "VERSION": b"1.5.5\n",
                "Dockerfile": b"FROM scratch\n",
                "docker-compose.yml": b"services: {}\n",
                "main.py": b"# fixture\n",
                "requirements.txt": b"\n",
                "src/main.py": b"# fixture\n",
                "config/setting_example.toml": b"synthetic='new'\n",
                "scripts/update_runtime.py": (ROOT / "scripts/update_runtime.py").read_bytes(),
            }
            # Record when the real update helper takes its snapshot/applies
            # files. The event file is a test-only argument-free side channel.
            helper = contents["scripts/update_runtime.py"].decode("utf-8")
            helper = helper.replace(
                '    if args.action == "prepare":',
                "    if args.action in {'snapshot', 'apply'}:\n"
                "        import os\n"
                "        events = os.environ.get('LAY4FLOW_TEST_MIGRATION_EVENTS')\n"
                "        if events:\n"
                "            with open(events, 'a', encoding='utf-8') as stream:\n"
                "                stream.write(args.action + '\\n')\n"
                '    if args.action == "prepare":',
            )
            contents["scripts/update_runtime.py"] = helper.encode("utf-8")
            with tarfile.open(runtime, "w:gz") as archive:
                for name, value in contents.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(value)
                    archive.addfile(entry, io.BytesIO(value))
            digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
            (bundle / "runtime/SHA256SUMS").write_text(
                f"{digest}  {runtime.name}\n", encoding="ascii"
            )
            wrapper = fixture / "check.ps1"
            wrapper.write_text(
                r"""param([string]$RepoRoot,[string]$Fixture,[string]$Interpreter,[string]$Mode)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$global:mode=$Mode
$global:events=[Collections.Generic.List[string]]::new()
$global:ownedRunning=$Mode -ne 'foreign'
$global:foreignRunning=$true
$global:startedPort=0
$env:LAY4FLOW_TEST_MIGRATION_EVENTS=Join-Path $Fixture 'events.txt'
$InstallDir=Join-Path $Fixture 'installed'
$BundleRoot=Join-Path $Fixture 'bundle'
$LogPath=Join-Path $Fixture 'install.log'
$global:ownedSocket=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$global:ownedSocket.Start()
$occupied=([Net.IPEndPoint]$global:ownedSocket.LocalEndpoint).Port
$global:containerRows=@{}
$global:containerRows['owned-fixture']=[pscustomobject]@{Id='owned-fixture';Config=[pscustomobject]@{Labels=@{'com.docker.compose.project.working_dir'=$InstallDir;'com.docker.compose.project'='owned-project'}};State=[pscustomobject]@{Running=$true};Mounts=@()}
$global:containerRows['foreign-fixture']=[pscustomobject]@{Id='foreign-fixture';Config=[pscustomobject]@{Labels=@{'com.docker.compose.project.working_dir'=($InstallDir+'-foreign');'com.docker.compose.project'='foreign-project'}};State=[pscustomobject]@{Running=$true};Mounts=@()}
$envText="# synthetic`r`nLAY4FLOW_REMOTE_PORT=$occupied`r`nCOMPOSE_PROJECT_NAME=owned-project`r`nUNRELATED=keep-value`r`n"
[IO.File]::WriteAllText((Join-Path $InstallDir '.env'),$envText,[Text.UTF8Encoding]::new($false))
$global:originalEnv=[IO.File]::ReadAllBytes((Join-Path $InstallDir '.env'))
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction) return @() }
function Invoke-RestMethod { param($Uri,$TimeoutSec,$Headers) return [pscustomobject]@{backend_running=$true;version='1.5.4';update_active_requests=0;update_pending_imports=0} }
function docker {
  $values=@($args | ForEach-Object {[string]$_})
  $global:events.Add('docker '+($values -join ' '))
  $global:LASTEXITCODE=0
  if($values[0] -eq 'info'){return 'synthetic ready'}
  if($values[0] -eq 'ps'){
    if($global:mode -eq 'foreign'){return 'foreign-fixture'}
    if($global:ownedRunning){return @('owned-fixture','foreign-fixture')}
    return 'foreign-fixture'
  }
  if($values[0] -eq 'inspect'){
    $id=$values[-1]
    if(-not $global:containerRows.ContainsKey($id)){throw 'Unexpected inspection target'}
    $row=$global:containerRows[$id]
    if($id -eq 'owned-fixture'){$row.State.Running=$global:ownedRunning}else{$row.State.Running=$global:foreignRunning}
    $formatIndex=[array]::IndexOf($values,'--format')
    if($formatIndex -ge 0){
      $format=$values[$formatIndex+1]
      if($format -match 'Config.Labels'){return ($row.Config.Labels|ConvertTo-Json -Compress)}
      if($format -match 'State.Running'){return ([string]$row.State.Running).ToLowerInvariant()}
      if($format -match 'json'){return ($row|ConvertTo-Json -Depth 8 -Compress)}
    }
    return (ConvertTo-Json -InputObject @($row) -Depth 8 -Compress)
  }
  if($values[0] -in @('stop','start')){
    if($values -contains 'foreign-fixture'){throw 'A foreign container was modified'}
    if(-not ($values -contains 'owned-fixture')){throw 'Unknown mutation target'}
    $global:ownedRunning=$values[0] -eq 'start'
    Add-Content -LiteralPath $env:LAY4FLOW_TEST_MIGRATION_EVENTS -Value ('docker-'+$values[0]) -Encoding utf8
    if(-not $global:ownedRunning){$global:ownedSocket.Stop()}
    return 'owned-fixture'
  }
  throw ('Unexpected Docker call: '+($values -join ' '))
}
# Replace only the bundled interpreter binding with the test interpreter.
# All archive, migration ordering, ownership and rollback code stays shipped code.
$source=[IO.File]::ReadAllText((Join-Path $RepoRoot 'scripts/windows/native-install.ps1'))
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$parseErrors)
$binding=$ast.Find({param($n)$n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$python'},$false)
if(-not $binding){throw 'Interpreter assignment missing'}
$replacement='$python='''+$Interpreter.Replace("'","''")+''''
$source=$source.Substring(0,$binding.Extent.StartOffset)+$replacement+$source.Substring($binding.Extent.EndOffset)
$testInstaller=Join-Path $Fixture 'native-install-fixture.ps1'
[IO.File]::WriteAllText($testInstaller,$source,[Text.UTF8Encoding]::new($true))
$failure=''
try {
  & $testInstaller -BundleRoot $BundleRoot -InstallDir $InstallDir -ResolveConflicts -NoBrowser -NoShortcuts -LogPath $LogPath
} catch { $failure=$_.Exception.Message }
finally { $global:ownedSocket.Stop() }
if($Mode -eq 'fail'){
  if($failure -notmatch 'synthetic native launch failure'){throw ('Expected launch failure: '+$failure)}
  if(-not $global:ownedRunning){throw 'The owned Docker backend was not restarted after rollback'}
  if((Get-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Raw).Trim() -ne '1.5.4'){throw 'Rollback did not restore version'}
}else{
  if($failure){throw $failure}
  if((Get-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Raw).Trim() -ne '1.5.5'){throw 'New version was not installed'}
  if($Mode -eq 'foreign'){
    if($global:startedPort -eq $occupied -or $global:startedPort -le 0){throw 'Foreign listener was not avoided'}
    $saved=Get-Content -LiteralPath (Join-Path $InstallDir '.env') -Raw
    if(-not $saved.Contains('LAY4FLOW_REMOTE_PORT='+$global:startedPort) -or -not $saved.Contains('UNRELATED=keep-value')){throw 'Port persistence lost unrelated settings'}
  }else{
    if($global:ownedRunning){throw 'Old Docker backend is still running after migration'}
    if($global:startedPort -ne $occupied){throw 'Owned Docker port should have been reused'}
  }
}
if(-not $global:foreignRunning){throw 'Foreign container was stopped'}
if($Mode -ne 'foreign'){
  $observed=@(Get-Content -LiteralPath $env:LAY4FLOW_TEST_MIGRATION_EVENTS)
  $stopIndex=[array]::IndexOf($observed,'docker-stop')
  $snapshotIndex=[array]::IndexOf($observed,'snapshot')
  $applyIndex=[array]::IndexOf($observed,'apply')
  if($stopIndex -lt 0 -or $snapshotIndex -le $stopIndex -or $applyIndex -le $snapshotIndex){throw 'Old Docker backend was not stopped before snapshot/apply'}
}
if((Get-Content -LiteralPath (Join-Path $InstallDir 'data/account.txt') -Raw) -ne 'synthetic-account-keep'){throw 'Account state changed'}
if((Get-Content -LiteralPath (Join-Path $InstallDir 'config/setting.toml') -Raw).Trim() -ne "synthetic='keep'"){throw 'Private config changed'}
Write-Output ('Synthetic migration passed: '+$Mode)
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
                    str(wrapper),
                    "-RepoRoot",
                    str(ROOT),
                    "-Fixture",
                    str(fixture),
                    "-Interpreter",
                    sys.executable,
                    "-Mode",
                    mode,
                ],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_leftover_native_runtime_still_stops_owned_docker_before_migration(self):
        self.run_migration("mixed")

    def test_native_launch_failure_restores_owned_docker_backend_and_data(self):
        self.run_migration("fail")

    def test_foreign_listener_is_preserved_and_new_port_is_persisted(self):
        self.run_migration("foreign")


if __name__ == "__main__":
    unittest.main()
