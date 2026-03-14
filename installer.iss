; installer.iss — Inno Setup script for ReDrive Rider
; Download Inno Setup: https://jrsoftware.org/isinfo.php
; Build: Open this file in Inno Setup Compiler and click Compile (or use ISCC.exe)

#define AppName      "ReDrive Rider"
#define AppVersion   "1.0"
#define AppPublisher "blucrew"
#define AppURL       "https://github.com/blucrew/ReDrive"
#define AppExeName   "ReDriveRider.exe"

[Setup]
AppId={{E8B3A1C2-4F6D-4A2E-8C1F-9D7B3E5A2F8C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\ReDrive Rider
DefaultGroupName=ReDrive Rider
DisableProgramGroupPage=yes
LicenseFile=
OutputDir=dist\installer
OutputBaseFilename=ReDriveRider-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
WizardSmallImageFile=
UninstallDisplayName=ReDrive Rider
; Require Windows 10 or later
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; The single-file exe built by PyInstaller
Source: "dist\ReDriveRider.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\ReDrive Rider";    Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall ReDrive Rider"; Filename: "{uninstallexe}"
Name: "{commondesktop}\ReDrive Rider";   Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch ReDrive Rider"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
