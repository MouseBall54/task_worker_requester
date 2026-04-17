#define MyAppName "TaskWorkerRequester"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "youngmoon"
#define MyAppExeName "TaskWorkerRequester.exe"
#define MyDistDir "..\\dist\\TaskWorkerRequester"
#define MyIconFile "..\\assets\\task_worker_requester.ico"

[Setup]
AppId={{E2C1A58A-67B0-44B1-8AF6-3D2FD375B271}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
WizardStyle=modern
Compression=lzma
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=TaskWorkerRequesterSetup
SetupIconFile={#MyIconFile}
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
