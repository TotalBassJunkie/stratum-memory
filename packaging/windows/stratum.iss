; ===========================================================================
; Stratum-Memory - Inno Setup script (the `Stratum-<version>-setup.exe` asset).
;
; Compiled on the windows-latest runner by packaging/windows/build-exe.ps1:
;
;     pyinstaller --noconfirm --clean --distpath dist --workpath build `
;         packaging/windows/stratum.spec
;     ISCC.exe /DVersion=<version> `
;         /DExePath=<abs>\stratum.exe `
;         /DIconPath=<abs>\favicon.ico `
;         /O<dist> /FStratum-<version>-setup `
;         packaging/windows/stratum.iss
;
; `Version` is a preprocessor define - it is NEVER hard-coded here, and the
; output file name is composed from it (`OutputBaseFilename` = the literal
; "Stratum-" plus the Version variable plus "-setup", matching the
; `Stratum-<version>-setup.exe` naming contract in docs/distribution.md).
; When neither /DVersion nor STRATUM_VERSION is present the compile stops with
; #error instead of shipping a bogus name.
;
; Note that Inno Setup Preprocessor directives and constants both use braces;
; the `#define` lines below are preprocessor, the DefaultDirName value below is
; an Inno constant.
; ===========================================================================

; --------------------------------------------------------------- version ---
; The /D switch (ISCC's `--define`) always wins; the STRATUM_VERSION
; environment variable is the fallback for a plain `ISCC.exe <script>`. When
; neither is present the compile stops with #error, so a build can never ship
; an artefact with a missing or guessed version.
#ifdef Version
#else
  #if GetEnv("STRATUM_VERSION") != ""
    #define Version GetEnv("STRATUM_VERSION")
  #else
    #error Version is not defined. Compile with: ISCC.exe /DVersion=<version> packaging\windows\stratum.iss
  #endif
#endif

; ------------------------------------------------------------ input paths ---
; The /D switches override these; the defaults are relative to this script and
; therefore only work for a local build from the repository root.
#ifdef ExePath
#else
  #define ExePath "..\..\dist\stratum.exe"
#endif
#ifdef IconPath
#else
  #define IconPath "..\..\assets\logo\favicon.ico"
#endif

; The setup.exe carries a binary version resource; Inno Setup wants up to four
; dot-separated numbers, so a bare "0.1.0" becomes "0.1.0.0".
#define VersionInfoVersion Version + ".0"

; Ship the project's Apache-2.0 text as the license page when it is present.
; The page is simply omitted when LICENSE does not exist.
#ifdef LicenseFile
#else
  #if FileExists("..\..\LICENSE")
    #define LicenseFile "..\..\LICENSE"
  #endif
#endif

; ============================================================== [Setup] =====
[Setup]
; AppId is a FIXED identity used for the uninstall log and the
; HKCU\...\Uninstall\<AppId>_is1 registry key. It must never change between
; releases, otherwise upgrades stop being recognised and uninstall leaves the
; old entry behind. It is not shown to users.
AppId={{8B7F1C42-6D3A-4F5E-9A18-2C0E6D4B7A91}
AppName=Stratum-Memory
AppVersion={#Version}
AppVerName=Stratum-Memory {#Version}
AppPublisher=TotalBassJunkie
AppPublisherURL=https://github.com/TotalBassJunkie/stratum-memory
AppSupportURL=https://github.com/TotalBassJunkie/stratum-memory/issues
AppUpdatesURL=https://github.com/TotalBassJunkie/stratum-memory/releases

; The CLI is a plain user-level tool, so the default install is per-user and
; needs no UAC prompt. `/ALLUSERS` (or the mode dialog) switches to the
; administrative install mode, which is then per-machine. The autopf constant
; on DefaultDirName resolves to the user's Programs directory in user mode and
; to Program Files in admin mode, so one directive covers both.
DefaultDirName={autopf}\Stratum
DefaultGroupName=Stratum-Memory
DisableProgramGroupPage=yes
AllowNoIcons=yes

PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

; Ship x64 only: the asset name says -setup (not -setup-x86) and the bundled
; stratum.exe is a 64-bit build. `x64compatible` also covers Windows 11 on Arm.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir=.
OutputBaseFilename=Stratum-{#Version}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#VersionInfoVersion}

; Ship the project's Apache-2.0 text as the license page.
#ifdef LicenseFile
  LicenseFile={#LicenseFile}
#endif

; Uninstaller icon: Programs and Features shows the app icon, which PyInstaller
; embedded into stratum.exe.
UninstallDisplayIcon={app}\stratum.exe

; stratum.exe is a console program; nothing to run after the files are copied.
ChangesEnvironment=yes

; ============================================================== [Files] =====
[Files]
; The PyInstaller one-file build. `ignoreversion` because a frozen executable
; carries its own version resource and we do not want Setup to skip the copy on
; a same-version reinstall.
Source: "{#ExePath}"; DestDir: "{app}"; DestName: "stratum.exe"; Flags: ignoreversion

; ========================================================== [Registry] =====
[Registry]
; Put the application directory on PATH via the environment-variable registry
; values, which is the mechanism Explorer, cmd.exe and PowerShell all read.
; ChangesEnvironment=yes above makes Setup broadcast WM_SETTINGCHANGE
; afterwards, so newly started shells see it without a logoff.
;
;   * user install    -> HKCU\Environment  (no admin rights required)
;   * admin install   -> HKLM\...\Environment (HKA maps to HKLM in admin mode)
;
; The value keeps its original type (REG_EXPAND_SZ stays expandable) thanks to
; preservestringtype, and uninsdeletevalue removes the entry on uninstall.
; ValueData appends the new directory to whatever was there before, and
; tolerates a missing or empty previous value.
Root: HKA; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Flags: preservestringtype uninsdeletevalue; Check: NeedsAddPath

; ============================================================ [Icons] =====
[Icons]
Name: "{group}\Stratum-Memory"; Filename: "{app}\stratum.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall Stratum-Memory"; Filename: "{uninstallexe}"

; ============================================================ [Code] ======
[Code]
{ True when the application directory is not already an entry of Path, so
  Setup does not add a duplicate on a reinstall or repair. }
function NeedsAddPath(Param: string): Boolean;
var
  Existing: string;
begin
  if not RegQueryStringValue(HKA, 'Environment', 'Path', Existing) then
    Existing := '';
  Result := Pos(';' + Uppercase(ExpandConstant('{app}')) + ';',
                ';' + Uppercase(Existing) + ';') = 0;
end;

{ Uninstall is handled declaratively: the [Registry] entry carries
  `uninsdeletevalue` (so the PATH entry goes away) and [Icons] entries are
  removed by Setup, while the uninstaller deletes the application directory and
  the Start-menu folder it created. Nothing else is left behind. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { Nothing to do - kept as the documented hook in case a future release has
      to clean up generated state (e.g. ~/.stratum). }
  end;
end;
