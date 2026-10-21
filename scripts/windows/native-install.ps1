[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$BundleRoot,
  [Parameter(Mandatory=$true)][string]$InstallDir,
  [int]$Port=38000,
  [switch]$NoBrowser,
  [switch]$NoShortcuts,
  [switch]$ResolveConflicts,
  [string]$LogPath=(Join-Path $env:TEMP 'lay4flow-remote-install.log')
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$BundleRoot=[IO.Path]::GetFullPath($BundleRoot)
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
if ($InstallDir.TrimEnd('\') -eq [IO.Path]::GetPathRoot($InstallDir).TrimEnd('\') -or $InstallDir.TrimEnd('\') -eq $env:WINDIR.TrimEnd('\')) { throw 'Choose an application folder as the installation directory.' }
$python=Join-Path $BundleRoot 'backend\windows-x64\python.exe'
$archive=@(Get-ChildItem -LiteralPath (Join-Path $BundleRoot 'runtime') -Filter '*-runtime.tar.gz' -File)
if($archive.Count -ne 1){throw 'Exactly one runtime archive is required.'}
$checksums=Join-Path $BundleRoot 'runtime\SHA256SUMS'
$archive=$archive[0].FullName
$envPath=Join-Path $InstallDir '.env'
$existing=Test-Path -LiteralPath (Join-Path $InstallDir 'VERSION')
$oldNative=Test-Path -LiteralPath (Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64\python.exe')
$stage=Join-Path ([IO.Path]::GetTempPath()) ('lay4flow-native-install-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$backup=$null
$changed=$false
$legacyStopped=$false
$maintenance=$false
$marker=Join-Path $InstallDir 'data\update-maintenance'
$nativeStopped=$false

function Log([string]$Message){Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8; Write-Host $Message}
function Run-Python([string[]]$Arguments){& $python @Arguments; if($LASTEXITCODE -ne 0){throw 'Native installer validation failed.'}}
function Copy-Tree([string]$Source,[string]$Target){New-Item -ItemType Directory -Force -Path $Target | Out-Null; Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Target -Recurse -Force}
function Start-Native { & (Join-Path $InstallDir '.lay4flow-windows\start.ps1') -InstallDir $InstallDir -Port $Port -NoBrowser }
function Check-Port([int]$Value){$socket=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Value); try{$socket.Start();return $true}catch{return $false}finally{$socket.Stop()}}

try {
  Log 'Installing the bundled Windows backend; Docker, Python and Node installations are not required.'
  # Verify the archive before extracting or modifying an existing installation.
  $expectedName=[IO.Path]::GetFileName($archive)
  $entries=@(Get-Content -LiteralPath $checksums | Where-Object {$_ -match ('^([a-fA-F0-9]{64})\s+\*?'+[regex]::Escape($expectedName)+'$')})
  if($entries.Count -ne 1 -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ine ($entries[0] -split '\s+')[0]){throw 'Bundled runtime checksum verification failed.'}
  $version=([string](& tar -xOf $archive VERSION)).Trim()
  if($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$'){throw 'Invalid bundled runtime version.'}
  if($existing){$oldVersion=(Get-Content -LiteralPath (Join-Path $InstallDir 'VERSION') -Raw).Trim(); if([version]$version -lt [version]$oldVersion){throw 'The installed version is newer; current files were preserved.'}}
  & tar -xzf $archive -C $stage scripts/update_runtime.py
  if($LASTEXITCODE -ne 0){throw 'Archive validator extraction failed.'}
  $helper=Join-Path $stage 'scripts\update_runtime.py'
  Copy-Item -LiteralPath $archive -Destination (Join-Path $stage 'runtime.tar.gz')
  Copy-Item -LiteralPath $checksums -Destination (Join-Path $stage 'SHA256SUMS')
  Run-Python @($helper,'prepare','--root',$InstallDir,'--stage',$stage,'--version',$version)
} catch {
  $failure=$_
  try { [IO.File]::WriteAllText(($LogPath+'.error.txt'),$failure.Exception.Message,[Text.UTF8Encoding]::new($true)) } catch {}
  $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
  if([IO.Path]::GetFullPath($stage).StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase)){Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue}
  throw
}

try {
  if(Test-Path -LiteralPath $envPath){$line=Select-String -LiteralPath $envPath -Pattern '^LAY4FLOW_REMOTE_PORT=(\d+)$' | Select-Object -First 1; if($line){$Port=[int]$line.Matches[0].Groups[1].Value}}
  if($existing){
    if(Test-Path -LiteralPath $marker){throw 'An update is already in progress.'}
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marker) | Out-Null
    [IO.File]::WriteAllText($marker,'native installer')
    $maintenance=$true
    try {$health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if($health.update_active_requests -gt 0 -or $health.update_pending_imports -gt 0){throw 'Complete active requests and imports before installing the update.'}} catch [Net.WebException] {}
    Run-Python @($helper,'idle','--root',$InstallDir)
    if($oldNative){& (Join-Path $InstallDir '.lay4flow-windows\native-stop.ps1') -InstallDir $InstallDir; $nativeStopped=$true}
    elseif(Test-Path -LiteralPath (Join-Path $InstallDir 'docker-compose.yml')){
      if(-not (Get-Command docker -ErrorAction SilentlyContinue)){
        throw 'An existing Docker installation was found, but Docker Desktop is not available. Start Docker Desktop once so the old service can be stopped safely, then retry the native installation.'
      }
      & (Join-Path $InstallDir '.lay4flow-windows\stop.ps1') -InstallDir $InstallDir
      $legacyStopped=$true
    }
    $backup=Join-Path $InstallDir ('.lay4flow-updates\native-'+[Guid]::NewGuid().ToString('N'))
    Run-Python @($helper,'snapshot','--root',$InstallDir,'--stage',$stage,'--backup',$backup)
    if(Test-Path -LiteralPath (Join-Path $InstallDir '.lay4flow-windows')){Copy-Tree (Join-Path $InstallDir '.lay4flow-windows') (Join-Path $backup 'windows')}
  } elseif(-not(Check-Port $Port)) {
    if(-not $ResolveConflicts){throw 'The local port is already in use.'}
    while($Port -lt 65535 -and -not(Check-Port $Port)){$Port++}
    if(-not(Check-Port $Port)){throw 'No available local port was found.'}
  }
  New-Item -ItemType Directory -Force -Path $InstallDir,(Join-Path $InstallDir 'config'),(Join-Path $InstallDir 'data'),(Join-Path $InstallDir 'tmp') | Out-Null
  $changed=$true
  Run-Python @($helper,'apply','--root',$InstallDir,'--stage',$stage)
  $windowsDir=Join-Path $InstallDir '.lay4flow-windows'
  Copy-Tree (Join-Path $BundleRoot 'windows') $windowsDir
  Copy-Tree (Join-Path $BundleRoot 'desktop\windows-x64') (Join-Path $windowsDir 'desktop')
  Copy-Tree (Join-Path $BundleRoot 'backend\windows-x64') (Join-Path $windowsDir 'backend\windows-x64')
  if(-not(Test-Path -LiteralPath (Join-Path $InstallDir 'config\setting.toml'))){Copy-Item -LiteralPath (Join-Path $InstallDir 'config\setting_example.toml') -Destination (Join-Path $InstallDir 'config\setting.toml')}
  if(-not(Test-Path -LiteralPath $envPath)){[IO.File]::WriteAllText($envPath,"LAY4FLOW_REMOTE_PORT=$Port`n",[Text.UTF8Encoding]::new($false))}
  Start-Native
  if(-not $NoShortcuts){
    $shell=New-Object -ComObject WScript.Shell
    $link=Join-Path ([Environment]::GetFolderPath('Desktop')) 'lay4flow-remote.lnk'
    $shortcut=$shell.CreateShortcut($link)
    if(-not $shortcut.WorkingDirectory -or [IO.Path]::GetFullPath($shortcut.WorkingDirectory).TrimEnd('\') -eq $InstallDir.TrimEnd('\')){
      $shortcut.TargetPath=Join-Path $windowsDir 'desktop\Lay4Flow.Desktop.exe'; $shortcut.Arguments='--install-dir "'+$InstallDir+'"'; $shortcut.WorkingDirectory=$InstallDir; $shortcut.IconLocation=(Join-Path $windowsDir 'tkcrm.ico')+',0';$shortcut.Save()
    }
  }
  Log "Native Windows installation ready: http://127.0.0.1:$Port"
  if(-not $NoBrowser){& (Join-Path $windowsDir 'start.ps1') -InstallDir $InstallDir -AlreadyStarted}
} catch {
  $failure=$_
  Log ('Installation failed: '+$failure.Exception.Message)
  [IO.File]::WriteAllText(($LogPath+'.error.txt'),$failure.Exception.Message,[Text.UTF8Encoding]::new($true))
  if($changed -and $backup){
    try {
      & (Join-Path $InstallDir '.lay4flow-windows\native-stop.ps1') -InstallDir $InstallDir
      Run-Python @($helper,'restore','--root',$InstallDir,'--stage',$stage,'--backup',$backup,'--restore-data')
      Copy-Tree (Join-Path $backup 'windows') (Join-Path $InstallDir '.lay4flow-windows')
      if($oldNative){Start-Native}
    } catch {Log ('Recovery needs attention: '+$_.Exception.Message)}
  }
  if($nativeStopped -and -not $changed){Start-Native}
  if($legacyStopped -and -not $oldNative){
    # A failed native migration may already have copied a bundled Python.
    # Preserve it outside the active launcher path so start.ps1 returns to the
    # restored Docker branch rather than launching the failed native backend.
    $failedBackend=Join-Path $InstallDir '.lay4flow-windows\backend'
    if(Test-Path -LiteralPath $failedBackend){
      if($backup){
        $preserved=Join-Path $backup 'failed-native-backend'
        $resolvedBackend=[IO.Path]::GetFullPath($failedBackend)
        $resolvedPreserved=[IO.Path]::GetFullPath($preserved)
        $expectedBackend=[IO.Path]::GetFullPath((Join-Path $InstallDir '.lay4flow-windows\backend'))
        $expectedBackup=[IO.Path]::GetFullPath($backup).TrimEnd('\')+'\'
        if($resolvedBackend -ne $expectedBackend -or -not $resolvedPreserved.StartsWith($expectedBackup,[StringComparison]::OrdinalIgnoreCase)){throw 'Native migration recovery path changed.'}
        if(-not(Test-Path -LiteralPath $preserved)){Move-Item -LiteralPath $resolvedBackend -Destination $resolvedPreserved}
      }
    }
    & (Join-Path $InstallDir '.lay4flow-windows\start.ps1') -InstallDir $InstallDir -NoBrowser
  }
  throw $failure
} finally {
  if($maintenance -and (Test-Path -LiteralPath $marker)){Remove-Item -LiteralPath $marker -Force}
  $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
  if([IO.Path]::GetFullPath($stage).StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase)){Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue}
}
