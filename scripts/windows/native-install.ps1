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
$stage=Join-Path ([IO.Path]::GetTempPath()) ('lay4flow-native-install-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$backup=$null
$changed=$false
$maintenance=$false
$marker=Join-Path $InstallDir 'data\update-maintenance'
$nativeStopped=$false
$nativeWasRunning=$false
$stoppedLegacyContainers=@()
$newStartAttempted=$false
$recoverySafe=$true
. (Join-Path $BundleRoot 'windows\native-process.ps1')
$launchMutex=Get-NativeMutex $InstallDir
$launchLocked=$false
$originalEnvExists=Test-Path -LiteralPath $envPath
$originalEnvBytes=if($originalEnvExists){[IO.File]::ReadAllBytes($envPath)}else{$null}
$portChanged=$false
$restorePort=$Port

function Log([string]$Message){Add-Content -LiteralPath $LogPath -Value $Message -Encoding UTF8; Write-Host $Message}
function Run-Python([string[]]$Arguments){
  Log ('Running bundled Python: "' + $python + '" ' + ($Arguments -join ' '))
  $savedErrorAction = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    $output = @(& $python @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
  } finally { $ErrorActionPreference = $savedErrorAction }
  foreach($line in $output){ if($line){ Log "Python: $line" } }
  Log "Bundled Python exit code: $exitCode"
  if($exitCode -ne 0){
    $detail = (($output | Where-Object {$_}) -join ' ').Trim()
    if(-not $detail){$detail='no diagnostic output'}
    throw "Native installer validation failed (Python exit code $exitCode): $detail"
  }
}
function Copy-Tree([string]$Source,[string]$Target){New-Item -ItemType Directory -Force -Path $Target | Out-Null; Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Target -Recurse -Force}
function Start-Native {
  # The package launcher also works after restoring an older installation that
  # does not yet support -AllowMaintenance or ownership recovery.
  & (Join-Path $BundleRoot 'windows\network.ps1') -InstallDir $InstallDir -Native | Out-Null
  & (Join-Path $BundleRoot 'windows\native-start.ps1') -InstallDir $InstallDir -Port $Port -NoBrowser -AllowMaintenance
}
function Check-Port([int]$Value){$socket=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Value); $socket.ExclusiveAddressUse=$true; try{$socket.Start();return $true}catch{return $false}finally{$socket.Stop()}}
function Save-NativePort([int]$Value){
  $text=if(Test-Path -LiteralPath $envPath){[IO.File]::ReadAllText($envPath)}else{''}
  $line='LAY4FLOW_REMOTE_PORT='+$Value
  if([regex]::IsMatch($text,'(?m)^\s*LAY4FLOW_REMOTE_PORT\s*=')){
    $text=[regex]::Replace($text,'(?m)^\s*LAY4FLOW_REMOTE_PORT\s*=.*$',$line)
  }else{
    if($text -and -not $text.EndsWith("`n")){$text+="`r`n"}
    $text+=$line+"`r`n"
  }
  $temporary=$envPath+'.port-'+[Guid]::NewGuid().ToString('N')
  try{
    [IO.File]::WriteAllText($temporary,$text,[Text.UTF8Encoding]::new($false))
    if(Test-Path -LiteralPath $envPath){[IO.File]::Replace($temporary,$envPath,[NullString]::Value)}
    else{[IO.File]::Move($temporary,$envPath)}
  }finally{if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Force}}
}
function Stop-InstalledNative {
  # Use this package's ownership-recovery script, even when upgrading an older
  # launcher that trusted only a stale or missing native-service.json file.
  & (Join-Path $BundleRoot 'windows\native-stop.ps1') -InstallDir $InstallDir
}
function Get-OwnedLegacyContainers {
  if(-not(Test-Path -LiteralPath (Join-Path $InstallDir 'docker-compose.yml'))){return @()}
  if(-not(Get-Command docker -ErrorAction SilentlyContinue)){
    # Every native package also contains docker-compose.yml. Its presence is
    # not proof of a legacy container on a machine without Docker installed.
    return @()
  }
  $savedErrorAction=$ErrorActionPreference
  try{
    $ErrorActionPreference='Continue'
    $ids=@(& docker ps -q --filter 'label=com.docker.compose.project' 2>$null)
    $listExit=$LASTEXITCODE
  }finally{$ErrorActionPreference=$savedErrorAction}
  if($listExit -ne 0){
    # An installed but stopped Docker engine cannot be holding this app's
    # mounts. An unrelated native listener is handled by the port fallback.
    return @()
  }
  $owned=@()
  foreach($id in $ids){
    if(-not $id){continue}
    $savedErrorAction=$ErrorActionPreference
    try{
      $ErrorActionPreference='Continue'
      $raw=@(& docker inspect $id 2>$null)
      $inspectExit=$LASTEXITCODE
    }finally{$ErrorActionPreference=$savedErrorAction}
    if($inspectExit -ne 0){throw 'Docker container ownership inspection failed; no application files were replaced.'}
    $details=@(($raw -join "`n") | ConvertFrom-Json)
    if($details.Count -ne 1){throw 'Docker container ownership metadata is invalid.'}
    $labels=$details[0].Config.Labels
    $ownerProperty=if($labels){$labels.PSObject.Properties['com.docker.compose.project.working_dir']}else{$null}
    if($ownerProperty -and (Test-NativePath ([string]$ownerProperty.Value) $InstallDir)){$owned += [string]$id}
  }
  return $owned
}
function Stop-InstalledLegacy {
  # Bundled native files may remain after an interrupted migration. They do
  # not prove the original Docker backend has stopped or released its mounts.
  foreach($id in @(Get-OwnedLegacyContainers)){
    Log "Stopping this installation's retained Docker backend $id before migration."
    $savedErrorAction=$ErrorActionPreference
    try{
      $ErrorActionPreference='Continue'
      & docker stop --time 90 $id | Out-Null
      $stopExit=$LASTEXITCODE
    }finally{$ErrorActionPreference=$savedErrorAction}
    if($stopExit -ne 0){throw 'The retained Docker backend could not stop; application files and data were preserved.'}
    $script:stoppedLegacyContainers += $id
  }
  if(@(Get-OwnedLegacyContainers).Count){throw 'The retained Docker backend is still running; application files and data were preserved.'}
}
function Restore-InstalledLegacy {
  foreach($id in $script:stoppedLegacyContainers){
    $savedErrorAction=$ErrorActionPreference
    try{
      $ErrorActionPreference='Continue'
      & docker start $id | Out-Null
      $restartExit=$LASTEXITCODE
    }finally{$ErrorActionPreference=$savedErrorAction}
    if($restartExit -ne 0){throw 'The previous Docker backend could not restart; its container and data were preserved.'}
  }
}

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
  $restorePort=$Port
  # Hold the launch lock across stop/copy/start. This also blocks an older
  # desktop launcher that does not yet understand update-maintenance.
  try{$launchLocked=$launchMutex.WaitOne(120000)}catch [Threading.AbandonedMutexException]{$launchLocked=$true}
  if(-not $launchLocked){throw 'Another launch is still starting the native backend.'}
  if($existing){
    if(Test-Path -LiteralPath $marker){throw 'An update is already in progress.'}
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marker) | Out-Null
    [IO.File]::WriteAllText($marker,'native installer')
    $maintenance=$true
    try {$health=Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3; if($health.update_active_requests -gt 0 -or $health.update_pending_imports -gt 0){throw 'Complete active requests and imports before installing the update.'}} catch [Net.WebException] {}
    Run-Python @($helper,'idle','--root',$InstallDir)
    $nativeWasRunning=@(Get-NativeOwnedProcesses $InstallDir).Count -gt 0
    Stop-InstalledNative
    $nativeStopped=$nativeWasRunning
    Stop-InstalledLegacy
    $backup=Join-Path $InstallDir ('.lay4flow-updates\native-'+[Guid]::NewGuid().ToString('N'))
    Run-Python @($helper,'snapshot','--root',$InstallDir,'--stage',$stage,'--backup',$backup)
    if(Test-Path -LiteralPath (Join-Path $InstallDir '.lay4flow-windows')){Copy-Tree (Join-Path $InstallDir '.lay4flow-windows') (Join-Path $backup 'windows')}
  }
  # Check only after the owned old backend has stopped. A foreign listener is
  # preserved; normal EXE installation selects and persists another free port.
  if(-not(Check-Port $Port)) {
    if(-not $ResolveConflicts){throw 'The local port is already in use.'}
    $previousPort=$Port
    while($Port -lt 65535 -and -not(Check-Port $Port)){$Port++}
    if(-not(Check-Port $Port)){throw 'No available local port was found.'}
    $portChanged=$true
    Log "Port $previousPort is occupied by another process; using available local port $Port."
  }
  New-Item -ItemType Directory -Force -Path $InstallDir,(Join-Path $InstallDir 'config'),(Join-Path $InstallDir 'data'),(Join-Path $InstallDir 'tmp') | Out-Null
  $changed=$true
  Run-Python @($helper,'apply','--root',$InstallDir,'--stage',$stage)
  $windowsDir=Join-Path $InstallDir '.lay4flow-windows'
  Copy-Tree (Join-Path $BundleRoot 'windows') $windowsDir
  Copy-Tree (Join-Path $BundleRoot 'desktop\windows-x64') (Join-Path $windowsDir 'desktop')
  Copy-Tree (Join-Path $BundleRoot 'backend\windows-x64') (Join-Path $windowsDir 'backend\windows-x64')
  if(-not(Test-Path -LiteralPath (Join-Path $InstallDir 'config\setting.toml'))){Copy-Item -LiteralPath (Join-Path $InstallDir 'config\setting_example.toml') -Destination (Join-Path $InstallDir 'config\setting.toml')}
  if($portChanged -or -not(Test-Path -LiteralPath $envPath)){Save-NativePort $Port}
  $newStartAttempted=$true
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
  if(-not $NoBrowser){& (Join-Path $windowsDir 'start.ps1') -InstallDir $InstallDir -AlreadyStarted -AllowMaintenance}
} catch {
  $failure=$_
  Log ('Installation failed: '+$failure.Exception.Message)
  [IO.File]::WriteAllText(($LogPath+'.error.txt'),$failure.Exception.Message,[Text.UTF8Encoding]::new($true))
  if($changed -and $backup){
    try {
      Stop-InstalledNative
      $restoreArguments=@($helper,'restore','--root',$InstallDir,'--stage',$stage,'--backup',$backup)
      if($newStartAttempted){$restoreArguments+='--restore-data'}
      Run-Python $restoreArguments
      Copy-Tree (Join-Path $backup 'windows') (Join-Path $InstallDir '.lay4flow-windows')
      if($portChanged -or $newStartAttempted){
        if($originalEnvExists){[IO.File]::WriteAllBytes($envPath,$originalEnvBytes)}
        elseif(Test-Path -LiteralPath $envPath){Remove-Item -LiteralPath $envPath -Force}
        $Port=$restorePort
      }
      if($nativeWasRunning -and -not $stoppedLegacyContainers.Count){Start-Native}
    } catch {$recoverySafe=$false;Log ('Recovery needs attention: '+$_.Exception.Message)}
  }
  if($nativeStopped -and -not $changed -and -not $stoppedLegacyContainers.Count){Start-Native}
  if($stoppedLegacyContainers.Count -and $recoverySafe){
    try{Restore-InstalledLegacy}catch{Log ('Previous Docker recovery needs attention: '+$_.Exception.Message)}
  }
  throw $failure
} finally {
  if($launchLocked){$launchMutex.ReleaseMutex()}
  $launchMutex.Dispose()
  if($maintenance -and (Test-Path -LiteralPath $marker)){Remove-Item -LiteralPath $marker -Force}
  $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
  if([IO.Path]::GetFullPath($stage).StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase)){Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue}
}
