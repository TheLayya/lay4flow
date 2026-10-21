[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallDir
)
$ErrorActionPreference = 'Stop'

function Resolve-SystemProxy {
  # PAC/WPAD lookup may wait on an unreachable configuration server. Isolate
  # the Windows resolver so startup keeps a bounded wait and can show a result.
  $code = @'
try {
  $target = [Uri]'https://accounts.google.com/'
  $resolved = [Net.WebRequest]::GetSystemWebProxy().GetProxy($target)
  if ($resolved -eq $target) { 'DIRECT' }
  elseif ($resolved -and $resolved.IsAbsoluteUri) { $resolved.AbsoluteUri }
} catch { exit 1 }
'@
  $options = [Diagnostics.ProcessStartInfo]::new()
  $options.FileName = Join-Path $PSHOME 'powershell.exe'
  if (-not (Test-Path -LiteralPath $options.FileName)) { $options.FileName = Join-Path $PSHOME 'pwsh.exe' }
  $options.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
  $options.UseShellExecute = $false
  $options.CreateNoWindow = $true
  $options.RedirectStandardOutput = $true
  $options.RedirectStandardError = $true
  $resolver = [Diagnostics.Process]::Start($options)
  try {
    $output = $resolver.StandardOutput.ReadToEndAsync()
    $errors = $resolver.StandardError.ReadToEndAsync()
    if (-not $resolver.WaitForExit(5000)) { $resolver.Kill(); return '' }
    if ($resolver.ExitCode -eq 0) { return $output.GetAwaiter().GetResult().Trim() }
    return ''
  } finally { $resolver.Dispose() }
}

function Get-ProxyCandidates {
  $values = [Collections.Generic.List[string]]::new()
  $settings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
  $usesPac = $settings -and $settings.PSObject.Properties['AutoConfigURL'] -and $settings.AutoConfigURL
  # Resolve the system decision first: a configured PAC may deliberately select
  # DIRECT for Google. Only scan listeners when there is no such explicit route.
  try {
    $resolvedValue = Resolve-SystemProxy
    $resolved = $null
    if ([Uri]::TryCreate($resolvedValue, [UriKind]::Absolute, [ref]$resolved)) {
      if ($resolved.Scheme -in @('http', 'https', 'socks5', 'socks5h')) {
        $values.Add($resolvedValue)
      }
    } elseif ($usesPac -and $resolvedValue -eq 'DIRECT') {
      return @()
    }
  } catch { }
  if ($settings -and $settings.PSObject.Properties['ProxyEnable'] -and $settings.ProxyEnable -eq 1 -and $settings.PSObject.Properties['ProxyServer'] -and $settings.ProxyServer) {
    foreach ($item in ([string]$settings.ProxyServer -split ';')) {
      $item = $item.Trim()
      if (-not $item) { continue }
      $scheme = 'http'
      if ($item -match '^(?<protocol>http|https|socks|socks5h?|socks4a?)\s*=\s*(?<value>.+)$') {
        $scheme = $Matches.protocol.ToLowerInvariant()
        $entry = $Matches.value.Trim()
      } else {
        $entry = $item
      }
      if ($scheme -eq 'socks') { $scheme = 'socks5' }
      # The bridge supports HTTP CONNECT and SOCKS5. Windows sometimes labels a
      # plain HTTP proxy as https=; retain it as HTTP so the first hop is still
      # probed instead of silently assuming a TLS proxy.
      if ($scheme -eq 'https') { $scheme = 'http' }
      if ($entry -notmatch '^[a-z][a-z0-9+.-]*://') { $entry = $scheme + '://' + $entry }
      if ($entry) { $values.Add($entry) }
    }
  }
  # TUN users may leave Windows system proxy disabled. Inspect the running proxy
  # application's actual listening sockets, rather than guessing common ports.
  $proxyProcesses = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '(clash|mihomo|sing-box|v2ray|xray|hysteria)' } | ForEach-Object { $_.Id })
  if ($proxyProcesses.Count) {
    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
      $_.OwningProcess -in $proxyProcesses -and $_.LocalPort -gt 0 -and $_.LocalAddress
    } | Sort-Object LocalPort, LocalAddress -Unique | ForEach-Object {
      $listenerHost = if ($_.LocalAddress -in @('0.0.0.0', '::')) { '127.0.0.1' } else { $_.LocalAddress }
      if ($listenerHost.Contains(':')) { $listenerHost = '[' + $listenerHost + ']' }
      $values.Add('http://' + $listenerHost + ':' + $_.LocalPort)
      $values.Add('socks5://' + $listenerHost + ':' + $_.LocalPort)
    }
  }
  return @($values | Select-Object -Unique)
}

function Read-ProxyBytes($Stream, [int]$Count, [int]$TimeoutMs = 2500) {
  $buffer = New-Object byte[] $Count
  $offset = 0
  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($offset -lt $Count) {
    $remaining = $TimeoutMs - [int]$clock.ElapsedMilliseconds
    if ($remaining -le 0) { return $null }
    $Stream.ReadTimeout = [Math]::Max(1, $remaining)
    $read = $Stream.Read($buffer, $offset, $Count - $offset)
    if ($read -le 0) { return $null }
    $offset += $read
  }
  return $buffer
}

function Test-HttpProxy([Uri]$ProxyUri, [int]$TimeoutMs = 6500) {
  if ($ProxyUri.Scheme -notin @('http', 'socks5', 'socks5h') -or $ProxyUri.UserInfo -or $ProxyUri.Port -lt 1) { return $false }
  if ($TimeoutMs -le 0) { return $false }
  $client = [Net.Sockets.TcpClient]::new()
  $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
  try {
    $connect = $client.ConnectAsync($ProxyUri.DnsSafeHost, $ProxyUri.Port)
    if (-not $connect.Wait([Math]::Min(1500, $TimeoutMs))) { return $false }
    $stream = $client.GetStream()
    $stream.ReadTimeout = 2500
    $stream.WriteTimeout = 1500
    if ($ProxyUri.Scheme -in @('socks5', 'socks5h')) {
      $stream.Write([byte[]]@(5,1,0), 0, 3)
      $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
      if ($remaining -le 0) { return $false }
      $response = Read-ProxyBytes $stream 2 ([Math]::Min(2500, $remaining))
      if (-not $response) { return $false }
      if ($response[0] -ne 5 -or $response[1] -ne 0) { return $false }
      $domain = [Text.Encoding]::ASCII.GetBytes('accounts.google.com')
      $request = [byte[]](@(5,1,0,3,$domain.Length) + $domain + @(1,187))
      $stream.Write($request, 0, $request.Length)
      $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
      if ($remaining -le 0) { return $false }
      $reply = Read-ProxyBytes $stream 4 ([Math]::Min(2500, $remaining))
      if (-not $reply -or $reply[0] -ne 5 -or $reply[1] -ne 0) { return $false }
      # Consume the complete SOCKS5 reply. A proxy that only sends the first
      # few bytes is not a usable transport, and accepting it creates a false
      # positive that later makes every browser request time out.
      $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
      if ($remaining -le 0) { return $false }
      switch ($reply[3]) {
        1 { $tail = Read-ProxyBytes $stream 6 ([Math]::Min(2500, $remaining)) }
        4 { $tail = Read-ProxyBytes $stream 18 ([Math]::Min(2500, $remaining)) }
        3 {
          $length = Read-ProxyBytes $stream 1 ([Math]::Min(2500, $remaining))
          if (-not $length) { return $false }
          $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
          if ($remaining -le 0) { return $false }
          $tail = Read-ProxyBytes $stream ($length[0] + 2) ([Math]::Min(2500, $remaining))
        }
        default { return $false }
      }
      return [bool]$tail
    }
    $request = [Text.Encoding]::ASCII.GetBytes("CONNECT accounts.google.com:443 HTTP/1.1`r`nHost: accounts.google.com:443`r`n`r`n")
    $stream.Write($request, 0, $request.Length)
    $buffer = New-Object byte[] 512
    $text = ''
    $clock = [Diagnostics.Stopwatch]::StartNew()
    for ($i = 0; $i -lt 8 -and $clock.ElapsedMilliseconds -lt 5000; $i++) {
      $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
      if ($remaining -le 0) { return $false }
      $stream.ReadTimeout = [Math]::Max(1, [Math]::Min(1000, $remaining))
      $count = $stream.Read($buffer, 0, $buffer.Length)
      if ($count -le 0) { break }
      $text += [Text.Encoding]::ASCII.GetString($buffer, 0, $count)
      if ($text.Contains("`r`n`r`n") -or $text.Length -ge 8192) { break }
    }
    return $text.Contains("`r`n`r`n") -and $text -match '^HTTP/\d(?:\.\d)?\s+200\b'
  } catch { return $false }
  finally { $client.Dispose() }
}

function Set-NetworkEnv([string]$Path, [string]$Value) {
  $content = if (Test-Path -LiteralPath $Path) { [IO.File]::ReadAllText($Path) } else { '' }
  $lines = @($content -split '\r?\n' | Where-Object { $_ -notmatch '^LAY4FLOW_HOST_PROXY=' -and $_ -ne '' })
  $lines += 'LAY4FLOW_HOST_PROXY=' + $Value
  [IO.File]::WriteAllText($Path, ($lines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
}

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$envPath = Join-Path $InstallDir '.env'
$mode = 'auto'
$manual = ''
if (Test-Path -LiteralPath $envPath) {
  foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line -match '^LAY4FLOW_HOST_PROXY_MODE=(auto|direct|manual)$') { $mode = $Matches[1] }
    if ($line -match '^LAY4FLOW_HOST_PROXY=(.*)$') { $manual = $Matches[1].Trim() }
  }
}
$selected = ''
$source = 'direct'
if ($mode -eq 'manual') {
  $manualUri = $null
  if (-not [Uri]::TryCreate($manual, [UriKind]::Absolute, [ref]$manualUri) -or $manualUri.Scheme -notin @('http', 'https', 'socks5', 'socks5h') -or -not $manualUri.Host -or $manualUri.Port -lt 1 -or $manualUri.Port -gt 65535 -or $manualUri.AbsolutePath -notin @('', '/') -or $manualUri.Query -or $manualUri.Fragment) {
    throw 'LAY4FLOW_HOST_PROXY manual mode requires a valid http(s):// or socks5(h):// host and port.'
  }
  $selected = $manual
  $source = 'manual'
} elseif ($mode -eq 'auto') {
  $candidates = @(Get-ProxyCandidates)
  $probeDeadline = [DateTime]::UtcNow.AddSeconds(15)
  foreach ($candidate in $candidates) {
    $remaining = [int]($probeDeadline - [DateTime]::UtcNow).TotalMilliseconds
    if ($remaining -le 0) { break }
    $uri = $null
    if ([Uri]::TryCreate($candidate, [UriKind]::Absolute, [ref]$uri) -and (Test-HttpProxy $uri ([Math]::Min(6500, $remaining)))) {
      $hostName = if ($uri.IsLoopback -or $uri.DnsSafeHost -in @('0.0.0.0', '::')) { 'host.docker.internal' } else { $uri.DnsSafeHost }
      if ($hostName.Contains(':')) { $hostName = '[' + $hostName + ']' }
      $selected = "$($uri.Scheme)://${hostName}:$($uri.Port)"
      $source = 'windows_proxy'
      break
    }
  }
}
Set-NetworkEnv $envPath $selected
$stateDir = Join-Path $InstallDir 'data'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$state = [pscustomobject]@{ mode = $mode; source = $source; upstream = $selected; checked_at = [DateTime]::UtcNow.ToString('o') }
$state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'host-network.json') -Encoding UTF8
Write-Output $state
