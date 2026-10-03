; Windows installer for Syntropy Health (Inno Setup 6). Built by .github/workflows/desktop.yml:
;   iscc /DAppVersion=1.2.3 desktop\installer.iss   (CI passes APP_VERSION from app/core/config.py)
; Installs for the current user (no administrator needed) into %LOCALAPPDATA%\Programs\Syntropy Health.
; Your data lives in %LOCALAPPDATA%\Syntropy Health and is kept when you uninstall.

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
function Relaunch(Mode: String): Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:relaunch|}') = Mode);
end;
