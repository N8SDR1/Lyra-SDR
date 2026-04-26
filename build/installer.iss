; Inno Setup script for Lyra-SDR
; -----------------------------------------------------------------
; Compile with:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
;
; Output:
;   dist\installer\Lyra-Setup-0.0.3.exe
;
; What this installer does:
;   - Installs the PyInstaller folder bundle from dist\Lyra\ into
;     the operator's chosen Program Files directory
;   - Creates Start menu shortcut + optional desktop shortcut
;   - Registers an Uninstall entry in Windows Add/Remove Programs
;   - Allows "Run Lyra now" checkbox after install completes
;   - Operator can install per-user (no admin) or system-wide (admin)
;
; What this installer does NOT do:
;   - Bundle Python — Lyra.exe contains the Python runtime via
;     PyInstaller, no separate Python install required
;   - Install device drivers — the HL2 uses standard Ethernet,
;     no driver work needed
;   - Modify QSettings registry entries — those land at first
;     launch under HKEY_CURRENT_USER\Software\N8SDR\Lyra
;   - Run any service / scheduled task / startup hook

#define LyraVersion       "0.0.3"
#define LyraVersionName   "First Tester Build"
#define LyraBuildDate     "2026-04-25"
#define LyraPublisher     "Rick Langford (N8SDR)"
#define LyraURL           "https://github.com/N8SDR1/Lyra-SDR"
#define LyraExeName       "Lyra.exe"

[Setup]
; Unique AppId — DO NOT CHANGE THIS between releases or operators
; will end up with TWO Lyra entries in Add/Remove Programs.
AppId={{C6F3A218-5D2B-4C1F-A9E1-3F8B7E6D2A41}
AppName=Lyra-SDR
AppVersion={#LyraVersion}
AppVerName=Lyra-SDR {#LyraVersion} ({#LyraVersionName})
AppPublisher={#LyraPublisher}
AppPublisherURL={#LyraURL}
AppSupportURL={#LyraURL}/issues
AppUpdatesURL={#LyraURL}/releases
DefaultDirName={autopf}\Lyra-SDR
DefaultGroupName=Lyra-SDR
DisableProgramGroupPage=yes
LicenseFile={#SourcePath}\..\LICENSE
OutputDir={#SourcePath}\..\dist\installer
OutputBaseFilename=Lyra-Setup-{#LyraVersion}
SetupIconFile={#SourcePath}\..\assets\logo\lyra.ico
UninstallDisplayIcon={app}\{#LyraExeName}
UninstallDisplayName=Lyra-SDR {#LyraVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Allow per-user install (no admin) OR system-wide (with admin)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
; Architecture: only build x64
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "quicklaunchicon"; Description: "Create a Quick &Launch shortcut"; \
    GroupDescription: "Additional shortcuts:"; \
    Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
; Source: the entire dist\Lyra folder built by PyInstaller.
; Destination: the operator's chosen install dir.
; Recurse + everything because PyInstaller folder-mode emits
; many subfolders (_internal/, etc.) that all need to ship.
Source: "{#SourcePath}\..\dist\Lyra\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start-menu shortcut — always
Name: "{autoprograms}\Lyra-SDR"; \
    Filename: "{app}\{#LyraExeName}"; \
    WorkingDir: "{app}"; \
    Comment: "Qt6 SDR transceiver for Hermes Lite 2 / 2+"

; Start-menu uninstall shortcut
Name: "{autoprograms}\Uninstall Lyra-SDR"; \
    Filename: "{uninstallexe}"; \
    Comment: "Uninstall Lyra-SDR"

; Optional desktop shortcut (off by default; operator opts in
; via the "Tasks" page during install)
Name: "{autodesktop}\Lyra-SDR"; \
    Filename: "{app}\{#LyraExeName}"; \
    WorkingDir: "{app}"; \
    Comment: "Qt6 SDR transceiver for Hermes Lite 2 / 2+"; \
    Tasks: desktopicon

[Run]
; Final-page "Run Lyra now" checkbox (off by default — operators
; usually want to read the readme / poke around before launching)
Filename: "{app}\{#LyraExeName}"; \
    Description: "Launch Lyra-SDR now"; \
    Flags: nowait postinstall skipifsilent unchecked

[Code]
// Friendly check — refuse to install over a still-running Lyra.exe
// (would fail anyway with "file in use" but the canned message is
// less helpful than telling the operator what to do).
function InitializeSetup(): Boolean;
begin
  Result := True;
  // Could add a process check here using FindWindowByClassName or
  // similar; keeping it simple for the first installer release.
end;
