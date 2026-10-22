; Inno Setup script for DeskAgent-Setup.exe.
; The GitHub workflow builds the app with PyInstaller first, then runs:
;   ISCC.exe /DAppVersion=1.0.N installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6F2B8C1E-4A7D-4E59-9B3A-2D51C7E0A8F4}
AppName=Desk Agent
AppVersion={#AppVersion}
AppPublisher=Desk Agent
DefaultDirName={autopf}\Desk Agent
DefaultGroupName=Desk Agent
DisableProgramGroupPage=yes
; Installs for the current user only, so no admin password is needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=DeskAgent-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\DeskAgent.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Put a Desk Agent icon on my desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\DeskAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The full source, so the code always travels with the app.
Source: "deskagent.py"; DestDir: "{app}\source"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}\source"; Flags: ignoreversion
Source: "build.bat"; DestDir: "{app}\source"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\Desk Agent"; Filename: "{app}\DeskAgent.exe"
Name: "{group}\Uninstall Desk Agent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Desk Agent"; Filename: "{app}\DeskAgent.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DeskAgent.exe"; Description: "Open Desk Agent now"; Flags: nowait postinstall skipifsilent
