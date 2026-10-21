#ifndef AppVersion
  #error Pass AppVersion from build_windows_setup.ps1
#endif
#ifndef BundleDir
  #error Pass the extracted Windows bundle directory
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef WebView2Bootstrapper
  #error Pass Microsoft-signed WebView2 bootstrapper path
#endif

[Setup]
AppId={{7A53958F-0D3A-4831-A9BE-B52BDF0D83C8}
AppName=lay4flow-remote
AppVersion={#AppVersion}
AppPublisher=TheLayya
DefaultDirName={localappdata}\lay4flow-remote
OutputDir={#OutputDir}
OutputBaseFilename=lay4flow-remote-{#AppVersion}-windows-x64-setup
SetupIconFile=tkcrm.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
PrivilegesRequired=lowest
WizardStyle=modern
Uninstallable=no
CreateAppDir=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
InfoBeforeFile={#BundleDir}\README.md
LicenseFile={#BundleDir}\LICENSE

[Files]
Source: "{#BundleDir}\*"; DestDir: "{tmp}\lay4flow-remote-bundle"; Flags: recursesubdirs createallsubdirs deleteafterinstall
Source: "{#WebView2Bootstrapper}"; Flags: dontcopy

[Code]
var
  InstallFailure: Integer;
  InstallLogPath: String;

function HasWebViewRuntime(Root: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(Root, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    and (Version <> '') and (Version <> '0.0.0.0');
end;

function IsWebView2Installed(): Boolean;
begin
  Result := HasWebViewRuntime(HKLM32) or HasWebViewRuntime(HKCU32) or HasWebViewRuntime(HKLM64) or HasWebViewRuntime(HKCU64);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if IsWebView2Installed() then Exit;
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
    Result := 'Microsoft Edge WebView2 Runtime setup did not start. Install the runtime and try again.'
  else if (ExitCode <> 0) or not IsWebView2Installed() then
    Result := 'Microsoft Edge WebView2 Runtime installation did not finish. Check the network and try again. Code: ' + IntToStr(ExitCode);
end;

procedure ReportInstallFailure(const Message: String);
var
  ErrorCode: Integer;
  ErrorLines: TArrayOfString;
  Details: String;
begin
  Details := Message;
  if LoadStringsFromFile(InstallLogPath + '.error.txt', ErrorLines) then
    if GetArrayLength(ErrorLines) > 0 then
      Details := ErrorLines[0];
  Log(Details);
  WizardForm.FinishedLabel.Caption := 'Installation did not complete.' + #13#10#13#10 + Details + #13#10#13#10 + 'Install log: ' + InstallLogPath;
  WizardForm.FinishedHeadingLabel.Caption := 'Installation needs attention';
  if not WizardSilent then begin
    if FileExists(InstallLogPath) then
      ShellExec('', ExpandConstant('{sys}\notepad.exe'), '"' + InstallLogPath + '"', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
    MsgBox(Details + #13#10#13#10 + 'Install log: ' + InstallLogPath, mbError, MB_OK);
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := InstallFailure;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Parameters: String;
begin
  if CurStep <> ssPostInstall then Exit;
  Parameters := '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\lay4flow-remote-bundle\install.ps1') +
    '" -BundleRoot "' + ExpandConstant('{tmp}\lay4flow-remote-bundle') +
    '" -InstallDir "' + ExpandConstant('{app}') + '"';
  if ExpandConstant('{param:LAYPORT|}') <> '' then
    Parameters := Parameters + ' -Port ' + ExpandConstant('{param:LAYPORT|}');
  if ExpandConstant('{param:LAYPROJECT|}') <> '' then
    Parameters := Parameters + ' -ProjectName "' + ExpandConstant('{param:LAYPROJECT|}') + '"';
  InstallLogPath := AddBackslash(GetEnv('TEMP')) + 'lay4flow-remote-install.log';
  Parameters := Parameters + ' -LogPath "' + InstallLogPath + '" -ResolveConflicts';
  if WizardSilent then Parameters := Parameters + ' -NoBrowser -NoShortcuts';
  Log('Starting Docker install in ' + ExpandConstant('{app}'));
  if not ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      Parameters, '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode, nil) then
  begin
    InstallFailure := 1;
    ReportInstallFailure('PowerShell installation could not be started.');
    Exit;
  end;
  if ResultCode <> 0 then begin
    InstallFailure := ResultCode;
    ReportInstallFailure('PowerShell installer failed with code ' + IntToStr(ResultCode));
  end;
end;
