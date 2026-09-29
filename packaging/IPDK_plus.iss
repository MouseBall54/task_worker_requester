#define MyAppName "IPDK_plus"
; MyAppVersion is read from app\version.py, the single version source.
#define VersionFile FileOpen(AddBackslash(SourcePath) + "..\app\version.py")
#if !VersionFile
  #error Cannot open app\version.py
#endif
#define VersionPrefix 'APP_VERSION = "'
#sub ReadVersionLine
  #define VersionLine FileRead(VersionFile)
  #if Pos(VersionPrefix, VersionLine) == 1
    #define public MyAppVersion Copy(VersionLine, Len(VersionPrefix) + 1, Len(VersionLine) - Len(VersionPrefix) - 1)
  #endif
#endsub
#for {0; !Defined(MyAppVersion) && !FileEof(VersionFile); 0} ReadVersionLine
#expr FileClose(VersionFile)
#ifndef MyAppVersion
  #error APP_VERSION not found in app\version.py
#endif
#define MyAppPublisher "박영문"
#define MyAppExeName "IPDK_plus.exe"
#define MyDistDir "..\\dist\\IPDK_plus"
#define MyIconFile "..\\assets\\IPDK_plus.ico"
#define MyVCRedistExe "..\\packaging\\prereqs\\vc_redist.x64.exe"
#define MyUpdateUrl "\\12.56.53.186\ssa_new\sw\ipdk_plus"

[Setup]
AppId={{E2C1A58A-67B0-44B1-8AF6-3D2FD375B271}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppUpdatesURL={#MyUpdateUrl}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
WizardStyle=modern
Compression=lzma
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=IPDK_plusSetup_{#MyAppVersion}
SetupIconFile={#MyIconFile}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoTextVersion={#MyAppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyVCRedistExe}"; DestDir: "{tmp}"; DestName: "vc_redist.x64.exe"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\업데이트 확인"; Filename: "{#MyUpdateUrl}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  VCRedistSuccess = 0;
  VCRedistAlreadyInstalled = 1638;
  VCRedistSuccessRebootRequired = 3010;
  VCRedistSuccessRebootInitiated = 1641;

procedure InstallVCRedistOrFail();
var
  ResultCode: Integer;
  InstallerPath: String;
begin
  InstallerPath := ExpandConstant('{tmp}\vc_redist.x64.exe');
  if not FileExists(InstallerPath) then
    RaiseException('Microsoft Visual C++ Redistributable installer was not found: ' + InstallerPath);

  Log('Starting VC++ Redistributable install: ' + InstallerPath);
  if not Exec(
    InstallerPath,
    '/install /quiet /norestart',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
    RaiseException('Failed to execute Microsoft Visual C++ Redistributable installer.');

  Log('VC++ Redistributable exit code: ' + IntToStr(ResultCode));
  if
    (ResultCode <> VCRedistSuccess) and
    (ResultCode <> VCRedistAlreadyInstalled) and
    (ResultCode <> VCRedistSuccessRebootRequired) and
    (ResultCode <> VCRedistSuccessRebootInitiated)
  then
    RaiseException('Microsoft Visual C++ Redistributable installation failed. Exit code: ' + IntToStr(ResultCode));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    InstallVCRedistOrFail();
end;
