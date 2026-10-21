param(
  [Parameter(Mandatory = $true)][long]$WindowHandle,
  [string]$MenuName,
  [string]$Screenshot,
  [switch]$FromScreen
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Drawing
$window = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
if ($null -eq $window) { throw 'The smoke-test desktop window does not exist.' }
if ($MenuName) {
  $condition = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty, $MenuName)
  $item = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
  if ($null -eq $item) {
    $all = $window.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
    $names = @($all | ForEach-Object { $_.Current.Name }) -join ' | '
    throw "Native menu item was not found: $MenuName. Available: $names"
  }
  $pattern = $item.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
  $pattern.Invoke()
}
if ($Screenshot) {
  Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class DesktopSmokeNative {
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr handle, out Rect rect);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr handle, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr handle, int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
}
'@
  if ($FromScreen) {
    [DesktopSmokeNative]::ShowWindow([IntPtr]$WindowHandle, 9) | Out-Null
    [DesktopSmokeNative]::SetForegroundWindow([IntPtr]$WindowHandle) | Out-Null
    Start-Sleep -Milliseconds 800
  }
  $rectangle = New-Object DesktopSmokeNative+Rect
  if (-not [DesktopSmokeNative]::GetWindowRect([IntPtr]$WindowHandle, [ref]$rectangle)) { throw 'Window bounds were not available.' }
  $bitmap = New-Object Drawing.Bitmap(($rectangle.Right - $rectangle.Left), ($rectangle.Bottom - $rectangle.Top))
  $graphics = [Drawing.Graphics]::FromImage($bitmap)
  $dc = $graphics.GetHdc()
  try {
    if (-not $FromScreen -and -not [DesktopSmokeNative]::PrintWindow([IntPtr]$WindowHandle, $dc, 2)) { throw 'Native window capture failed.' }
  } finally { $graphics.ReleaseHdc($dc); $graphics.Dispose() }
  if ($FromScreen) {
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    try { $graphics.CopyFromScreen($rectangle.Left, $rectangle.Top, 0, 0, $bitmap.Size) }
    finally { $graphics.Dispose() }
  }
  try { $bitmap.Save([IO.Path]::GetFullPath($Screenshot), [Drawing.Imaging.ImageFormat]::Png) }
  finally { $bitmap.Dispose() }
}
