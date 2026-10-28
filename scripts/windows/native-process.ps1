# Native process ownership is proven from exact executable and argument paths.
# PID state is only a cache: upgrades must also find a launcher whose state file
# was lost or whose original ShellExecute process handed off to another PID.
function ConvertFrom-NativeCommandLine([string]$CommandLine) {
  $values = @()
  foreach ($match in [regex]::Matches($CommandLine, '"([^"]*)"|(\S+)')) {
    if ($match.Groups[1].Success) { $values += $match.Groups[1].Value }
    else { $values += $match.Groups[2].Value }
  }
  return $values
}

function Get-NativeArgument([string[]]$Arguments, [string]$Name) {
  $matches = @()
  for ($index = 0; $index -lt $Arguments.Count; $index++) {
    if ($Arguments[$index] -ceq $Name) {
      if ($index + 1 -ge $Arguments.Count) { return $null }
      $matches += $Arguments[$index + 1]
    }
  }
  if ($matches.Count -ne 1) { return $null }
  return [string]$matches[0]
}

function Test-NativePath([string]$Actual, [string]$Expected) {
  if (-not $Actual) { return $false }
  try {
    return [IO.Path]::GetFullPath($Actual).TrimEnd('\').Equals(
      [IO.Path]::GetFullPath($Expected).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
  } catch { return $false }
}

function Get-NativeOwnedProcesses([string]$InstallDir) {
  $expectedPython = Join-Path $InstallDir '.lay4flow-windows\backend\windows-x64\python.exe'
  $expectedService = Join-Path $InstallDir '.lay4flow-windows\native-service.py'
  $expectedStop = Join-Path $InstallDir 'data\native-service.stop'
  $expectedLog = Join-Path $InstallDir 'data\native-service.log'
  $owned = @()
  foreach ($candidate in @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop)) {
    if (-not $candidate -or -not $candidate.ExecutablePath -or -not $candidate.CommandLine) { continue }
    if (-not (Test-NativePath ([string]$candidate.ExecutablePath) $expectedPython)) { continue }
    $arguments = @(ConvertFrom-NativeCommandLine ([string]$candidate.CommandLine))
    if ($arguments.Count -lt 2 -or -not (Test-NativePath $arguments[1] $expectedService)) { continue }
    if (-not (Test-NativePath (Get-NativeArgument $arguments '--root') $InstallDir)) { continue }
    if (-not (Test-NativePath (Get-NativeArgument $arguments '--stop-file') $expectedStop)) { continue }
    if (-not (Test-NativePath (Get-NativeArgument $arguments '--log-file') $expectedLog)) { continue }
    $portValue = 0
    if (-not [int]::TryParse((Get-NativeArgument $arguments '--port'), [ref]$portValue) -or $portValue -lt 1 -or $portValue -gt 65535) { continue }
    try {
      $process = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction Stop
      if ($process.HasExited) { continue }
      $owned += [pscustomobject]@{
        Process = $process
        Port = $portValue
        CreationTicks = [string]$process.StartTime.ToUniversalTime().Ticks
      }
    } catch { continue }
  }
  return $owned
}

function Get-NativeMutex([string]$InstallDir) {
  $algorithm = [Security.Cryptography.SHA256]::Create()
  try {
    $key = ([BitConverter]::ToString($algorithm.ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallDir.ToUpperInvariant())))).Replace('-', '').Substring(0, 24)
  } finally { $algorithm.Dispose() }
  return [Threading.Mutex]::new($false, ('Local\lay4flow-native-start-' + $key))
}
