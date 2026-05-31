; PTZ-Control Windows installer (Inno Setup 6)
#define MyAppName "PTZ-Control"
#define MyAppVersion "3.25"
#define MyAppPublisher "PTZ-Control"
#define MyAppExeName "PTZ-Control.exe"
#define MyAppURL "https://github.com/Danieldsavn/PTZ-Control"

[Setup]
AppId={{A7B3E4F1-9C2D-4E8A-B5F6-PTZCONTROL01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=PTZ-Control-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "webview2"; Description: "Install Microsoft WebView2 Runtime (required for the app UI)"; GroupDescription: "Additional components"
Name: "vcredist"; Description: "Install Visual C++ 2015-2022 Redistributable (x64)"; GroupDescription: "Additional components"
Name: "ffmpeg"; Description: "Install FFmpeg for live camera previews (optional)"; GroupDescription: "Additional components"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\PTZ-Control-Updater.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\version.json"; DestDir: "{app}"; Flags: onlyifdoesntexist ignoreversion
Source: "deps\ffmpeg.exe"; DestDir: "{app}\tools"; Tasks: ffmpeg; Flags: ignoreversion skipifsourcedoesntexist
Source: "deps\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Tasks: webview2; Flags: deleteafterinstall skipifsourcedoesntexist; Check: NeedsWebView2
Source: "deps\vc_redist.x64.exe"; DestDir: "{tmp}"; Tasks: vcredist; Flags: deleteafterinstall skipifsourcedoesntexist; Check: NeedsVCRedist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing WebView2 Runtime..."; Tasks: webview2; Flags: waituntilterminated; Check: NeedsWebView2AndTask
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Visual C++ Redistributable..."; Tasks: vcredist; Flags: waituntilterminated; Check: NeedsVCRedistAndTask
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\apply_update.log"
Type: files; Name: "{app}\update_job.json"
Type: files; Name: "{app}\*.download"
Type: files; Name: "{app}\*.bak"

[Code]
function CompareVersion(V1, V2: String): Integer;
var
  P, N1, N2: Integer;
  S1, S2: String;
begin
  S1 := V1;
  S2 := V2;
  while (S1 <> '') or (S2 <> '') do
  begin
    P := Pos('.', S1);
    if P = 0 then begin N1 := StrToIntDef(S1, 0); S1 := ''; end
    else begin N1 := StrToIntDef(Copy(S1, 1, P - 1), 0); Delete(S1, 1, P); end;
    P := Pos('.', S2);
    if P = 0 then begin N2 := StrToIntDef(S2, 0); S2 := ''; end
    else begin N2 := StrToIntDef(Copy(S2, 1, P - 1), 0); Delete(S2, 1, P); end;
    if N1 < N2 then begin Result := -1; Exit; end;
    if N1 > N2 then begin Result := 1; Exit; end;
  end;
  Result := 0;
end;

function WebView2Installed: Boolean;
var Ver: String;
begin
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Ver) then
    Result := (CompareVersion(Ver, '96.0.1054.62') >= 0)
  else if RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Ver) then
    Result := (CompareVersion(Ver, '96.0.1054.62') >= 0)
  else
    Result := False;
end;

function NeedsWebView2: Boolean;
begin
  Result := not WebView2Installed;
end;

function NeedsWebView2AndTask: Boolean;
begin
  Result := NeedsWebView2 and WizardIsTaskSelected('webview2');
end;

function VCRedistInstalled: Boolean;
var Installed: Cardinal;
begin
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
  begin
    Result := (Installed = 1);
    Exit;
  end;
  if RegQueryDWordValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) then
  begin
    Result := (Installed = 1);
    Exit;
  end;
  Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64');
end;

function NeedsVCRedist: Boolean;
begin
  Result := not VCRedistInstalled;
end;

function NeedsVCRedistAndTask: Boolean;
begin
  Result := NeedsVCRedist and WizardIsTaskSelected('vcredist');
end;

function FFmpegAlreadyAvailable: Boolean;
var AppDir: String;
  ResultCode: Integer;
begin
  AppDir := ExpandConstant('{app}');
  if (AppDir <> '') and FileExists(AppDir + '\tools\ffmpeg.exe') then
  begin
    Result := True;
    Exit;
  end;
  if Exec('cmd.exe', '/c where ffmpeg >nul 2>&1', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := (ResultCode = 0)
  else
    Result := False;
end;

function NeedsFFmpeg: Boolean;
begin
  Result := not FFmpegAlreadyAvailable;
end;

function ShouldHideTask(Index: Integer): Boolean;
begin
  { Task order: 0=webview2, 1=vcredist, 2=ffmpeg, 3=desktopicon }
  case Index of
    0: Result := not NeedsWebView2;
    1: Result := not NeedsVCRedist;
    2: Result := not NeedsFFmpeg;
  else
    Result := False;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
var AnyDepVisible: Boolean;
begin
  Result := False;
  if PageID = wpSelectTasks then
  begin
    AnyDepVisible := NeedsWebView2 or NeedsVCRedist or NeedsFFmpeg;
    { If every dependency is already satisfied, skip straight past this page (desktop shortcut uses default). }
    if not AnyDepVisible then
      Result := True;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var TasksToSelect: String;
begin
  if CurPageID = wpSelectTasks then
  begin
    TasksToSelect := 'desktopicon';
    if NeedsWebView2 then
      TasksToSelect := TasksToSelect + ' webview2';
    if NeedsVCRedist then
      TasksToSelect := TasksToSelect + ' vcredist';
    if NeedsFFmpeg then
      TasksToSelect := TasksToSelect + ' ffmpeg';
    WizardSelectTasks(Trim(TasksToSelect));
  end
  else if CurPageID = wpReady then
  begin
    { Tasks page was skipped — ensure desktop shortcut stays enabled by default }
    if not WizardIsTaskSelected('desktopicon') then
      WizardSelectTasks('desktopicon');
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpReady then
  begin
    if NeedsWebView2 and (not ShouldHideTask(0)) and not WizardIsTaskSelected('webview2') then
    begin
      if MsgBox('PTZ-Control requires the Microsoft WebView2 Runtime to display its interface.' + #13#10 +
        'Install WebView2 is unchecked. Continue anyway?', mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
    if NeedsVCRedist and (not ShouldHideTask(1)) and not WizardIsTaskSelected('vcredist') then
    begin
      if MsgBox('Visual C++ Redistributable is recommended. The app may not start without it.' + #13#10 +
        'Continue without installing it?', mbConfirmation, MB_YESNO) = IDNO then
        Result := False;
    end;
  end;
end;
