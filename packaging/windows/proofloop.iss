; Inno Setup script: installs the PyInstaller folder (packaging/windows/proofloop.spec) for the current
; user only, with no admin rights, under %LOCALAPPDATA%\Programs\ProofLoop.
;   ISCC.exe /DAppVersion=0.4.0 [/DIconFile=build\pyinstaller\proofloop.ico] packaging\windows\proofloop.iss
; Gives dist\installer\ProofLoop-Setup-0.4.0.exe. Settings and data stay in %USERPROFILE%\.proofloop
; (installing, updating and uninstalling never touch them).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define Root "..\.."
#define Dist Root + "\dist\ProofLoop"

[Setup]
AppId={{FD03DBB2-F118-40DF-888C-D253CC37793A}
AppName=ProofLoop
AppVersion={#AppVersion}
AppVerName=ProofLoop {#AppVersion}
AppPublisher=ProofLoop contributors
AppPublisherURL=https://github.com/jmxsmith2-hash/Proof-loop
AppSupportURL=https://github.com/jmxsmith2-hash/Proof-loop/issues
AppUpdatesURL=https://github.com/jmxsmith2-hash/Proof-loop/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\ProofLoop
DefaultGroupName=ProofLoop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile={#Root}\LICENSE
OutputDir={#Root}\dist\installer
OutputBaseFilename=ProofLoop-Setup-{#AppVersion}
UninstallDisplayIcon={app}\ProofLoopApp.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef IconFile
SetupIconFile={#IconFile}
#endif

[Tasks]
Name: "desktopicon"; Description: "Put a ProofLoop shortcut on the desktop"; Flags: unchecked
Name: "autostart"; Description: "Start ProofLoop in the background when I log in (so your printers stay watched)"

[Files]
Source: "{#Dist}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\ProofLoop"; Filename: "{app}\ProofLoopApp.exe"; Comment: "Check designs and G-code, make loop files, watch your printers"
Name: "{group}\Uninstall ProofLoop"; Filename: "{uninstallexe}"
Name: "{autodesktop}\ProofLoop"; Filename: "{app}\ProofLoopApp.exe"; Tasks: desktopicon
; The same shortcut "proofloop autostart on" makes, so ProofLoop's own "Start when I log in" switch sees it.
Name: "{userstartup}\ProofLoop"; Filename: "{app}\ProofLoopApp.exe"; Parameters: "service"; Tasks: autostart; Comment: "ProofLoop in the background: watches your printers"

[Run]
Filename: "{app}\ProofLoopApp.exe"; Description: "Open ProofLoop"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\proofloop.exe"; Parameters: "stop"; Flags: runhidden; RunOnceId: "StopProofLoop"

[UninstallDelete]
; If "Start when I log in" was switched on from ProofLoop itself rather than by this installer.
Type: files; Name: "{userstartup}\ProofLoop.lnk"

[Code]
// A running ProofLoop holds its files open: quit it first (an update installs over the old folder).
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\proofloop.exe')) then
    Exec(ExpandConstant('{app}\proofloop.exe'), 'stop', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Result := '';
end;
