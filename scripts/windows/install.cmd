@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -BundleRoot "%~dp0." -ResolveConflicts
if errorlevel 1 (
  echo.
  echo Installation failed. Review the error above, then press any key to close.
  pause >nul
)
endlocal
