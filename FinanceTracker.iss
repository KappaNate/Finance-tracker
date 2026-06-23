[Setup]
AppName=Finance Tracker
AppVersion=1.1.4
AppPublisher=KappaNate
DefaultDirName={autopf}\Finance Tracker
DefaultGroupName=Finance Tracker
OutputDir=installer
OutputBaseFilename=FinanceTracker_Setup_1.1.4
Compression=lzma
SolidCompression=yes
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Finance Tracker.exe
CloseApplications=yes

[Files]
Source: "dist\Finance Tracker.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Finance Tracker"; Filename: "{app}\Finance Tracker.exe"
Name: "{commondesktop}\Finance Tracker"; Filename: "{app}\Finance Tracker.exe"

[Run]
Filename: "{app}\Finance Tracker.exe"; Description: "Launch Finance Tracker"; Flags: nowait postinstall skipifsilent
