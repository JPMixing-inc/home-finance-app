; Inno Setup recipe for HomeFinance-Windows-Setup.exe. Built by build-windows.bat, which passes
; /DAppVersion=<app/version.py>. Installs for the current user only (no admin prompt), into
; %LOCALAPPDATA%\Programs\Home Finance, so the app can update itself there.
; The app's self-updater runs this silently with /DIR set to the folder it's running from.

#ifndef AppVersion
  #error Pass /DAppVersion=1.2.3 (build-windows.bat does this)
#endif

[Setup]
; Never change AppId: Windows uses it to recognise upgrades and the uninstall entry.
AppId={{DA0407F7-EAA0-4CAB-89DC-8D0216CFA329}
AppName=Home Finance
AppVersion={#AppVersion}
AppVerName=Home Finance {#AppVersion}
AppPublisher=Home Finance
AppPublisherURL=https://github.com/JPMixing-inc/home-finance-app
AppSupportURL=https://github.com/JPMixing-inc/home-finance-app/issues
AppUpdatesURL=https://github.com/JPMixing-inc/home-finance-app/releases
VersionInfoVersion={#AppVersion}
SourceDir=..
OutputDir=dist
OutputBaseFilename=HomeFinance-Windows-Setup
SetupIconFile=packaging\icon.ico
UninstallDisplayIcon={app}\Home Finance.exe
UninstallDisplayName=Home Finance
DefaultDirName={localappdata}\Programs\Home Finance
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Clear out the old version's libraries so nothing stale is left behind after an upgrade.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\Home Finance\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Home Finance"; Filename: "{app}\Home Finance.exe"
Name: "{autodesktop}\Home Finance"; Filename: "{app}\Home Finance.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Home Finance.exe"; Description: "{cm:LaunchProgram,Home Finance}"; Flags: nowait postinstall skipifsilent
