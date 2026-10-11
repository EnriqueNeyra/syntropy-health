; Windows installer for Syntropy Health (Inno Setup 6). Built by .github/workflows/desktop.yml:
;   iscc /DAppVersion=1.2.3 desktop\installer.iss   (CI passes APP_VERSION from app/core/config.py)
; Installs for the current user (no administrator needed) into %LOCALAPPDATA%\Programs\Syntropy Health.
; Your data lives in %LOCALAPPDATA%\Syntropy Health. Uninstalling asks whether to delete it too (keeping it is the
; default, and silent uninstalls keep it: install.ps1 -Uninstall -Purge deletes it itself).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1F3E5A-2B7D-4C8E-9A41-5D3B8F0E7C21}
AppName=Syntropy Health
AppVersion={#AppVersion}
AppPublisher=Syntropy Labs
AppPublisherURL=https://health.syntropylabs.io
DefaultDirName={localappdata}\Programs\Syntropy Health
DefaultGroupName=Syntropy Health
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=Syntropy-Health-{#AppVersion}-windows-x64-setup
SetupIconFile=icons\icon.ico
UninstallDisplayIcon={app}\Syntropy Health.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
LicenseFile=..\LICENSE

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startup"; Description: "Open Syntropy Health when I sign in (keeps it reachable for the iPhone app)"; GroupDescription: "Background:"; Flags: unchecked

[Files]
Source: "dist\Syntropy Health\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Syntropy Health"; Filename: "{app}\Syntropy Health.exe"
Name: "{autodesktop}\Syntropy Health"; Filename: "{app}\Syntropy Health.exe"; Tasks: desktopicon

[Registry]
; The app manages this value itself (tray icon → Open at Login); remove it when uninstalling.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Syntropy Health"; ValueData: """{app}\Syntropy Health.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "Syntropy Health"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\Syntropy Health.exe"; Description: "Open Syntropy Health"; Flags: nowait postinstall skipifsilent
; An update the app installed itself (silently, see desktop/updater.py): open it again the way it was.
Filename: "{app}\Syntropy Health.exe"; Parameters: "--background"; Flags: nowait; Check: Relaunch('background')
Filename: "{app}\Syntropy Health.exe"; Flags: nowait; Check: Relaunch('window')

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""Syntropy Health.exe"""; Flags: runhidden; RunOnceId: "StopApp"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

function Relaunch(Mode: String): Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|}') = Mode);
end;

// Anything still running from the app's folder (the console syntropy-health.exe an AI app started for its MCP server,
// say) would keep its files from being removed. And `syntropy-health service install`, run from this app, starts the
// server at sign-in: a scheduled task or a Run value that can't outlive the app. Ones pointing elsewhere (a server
// installed with install.ps1 -Server) are left alone.
procedure StopWhatRunsFromApp;
var
  App, Command, Script: String;
  Code: Integer;
begin
  App := ExpandConstant('{app}') + '\';
  if RegQueryStringValue(HKCU, RunKey, 'Syntropy Health Server', Command) and (Pos(Lowercase(App), Lowercase(Command)) > 0) then
    RegDeleteValue(HKCU, RunKey, 'Syntropy Health Server');
  StringChangeEx(App, '''', '''''', True);
  Script :=
    '$app = ''' + App + '''; ' +
    'Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($app, ''OrdinalIgnoreCase'') } | Stop-Process -Force -ErrorAction SilentlyContinue; ' +
    '$task = Get-ScheduledTask -TaskName ''Syntropy Health'' -ErrorAction SilentlyContinue; ' +
    'if ($task -and ($task.Actions | Where-Object { $_.Execute -and $_.Execute.Trim([char]34).StartsWith($app, ''OrdinalIgnoreCase'') })) ' +
    '{ Unregister-ScheduledTask -TaskName ''Syntropy Health'' -Confirm:$false }';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + Script + '"', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

// Your profiles, accounts and records are kept unless you say otherwise, so installing again picks up where you left
// off. Asked only when someone is watching: a silent uninstall keeps them.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
    StopWhatRunsFromApp;
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Data := ExpandConstant('{localappdata}\Syntropy Health');
  if not DirExists(Data) then
    Exit;
  if not UninstallSilent and (MsgBox('Also delete your Syntropy Health data from this PC?' + #13#10#13#10 +
      'Yes permanently deletes every profile, account, health record and setting Syntropy Health keeps on this PC. ' +
      'It can''t be undone.' + #13#10#13#10 +
      'No keeps them in ' + Data + ', so they''re there if you install Syntropy Health again.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
  begin
    if not DelTree(Data, True, True, True) then
      MsgBox('Some of your data couldn''t be deleted. Delete the folder ' + Data + ' yourself to finish.', mbError, MB_OK);
  end
  else
    DelTree(Data + '\updates', True, True, True);      // installers downloaded for updates
end;
