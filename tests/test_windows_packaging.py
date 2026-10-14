"""Tests for the Windows installer sources (WiX v4 MSI + Inno Setup EXE).

The two Windows installer assets are part of the release contract in
``docs/distribution.md``::

    Stratum-<version>-x64.msi   (WiX v4, windows runner)
    Stratum-<version>-setup.exe (Inno Setup, windows runner)

The suite pins that contract as executable expectations and is deliberately
**DB-free, network-free and side-effect-free**:

* every check only reads files under ``packaging/windows/`` and pattern-matches
  them — neither WiX nor Inno Setup has to be installed, and nothing is built,
* the two ``.ps1`` build scripts are additionally handed to the PowerShell
  language parser (skipped when no PowerShell is on ``PATH``), so a syntax error
  fails here instead of on the CI runner,
* no test executes a build script, downloads anything or needs a database.

The version rule from the task contract is asserted too: the artefact names must
be *composed* from the version at build time, never hard-coded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = REPO_ROOT / "packaging" / "windows"

WXS = PACKAGING_DIR / "stratum.wxs"
ISS = PACKAGING_DIR / "stratum.iss"
SPEC = PACKAGING_DIR / "stratum.spec"
ENTRY = PACKAGING_DIR / "stratum_entry.py"
BUILD_MSI = PACKAGING_DIR / "build-msi.ps1"
BUILD_EXE = PACKAGING_DIR / "build-exe.ps1"
README = PACKAGING_DIR / "README.md"

#: Every file this ticket owns and must ship.
REQUIRED_FILES = (WXS, ISS, SPEC, ENTRY, BUILD_MSI, BUILD_EXE, README)

#: The two build scripts, as ``(path, module)`` pairs for the parser check.
BUILD_SCRIPTS = (BUILD_MSI, BUILD_EXE)

#: Asset names fixed by docs/distribution.md, with ``<version>`` as the literal
#: placeholder the build scripts substitute.
MSI_ASSET = "Stratum-<version>-x64.msi"
EXE_ASSET = "Stratum-<version>-setup.exe"

#: A plausible version string; used only to prove a name is *composed*.
VERSION = "9.9.9"

#: Markers of a committed credential. Kept narrow on purpose: these files
#: legitimately mention PATH and environment variables, so a bare "secret"
#: match would be noise.
SECRET_PATTERNS = (
    re.compile(r"api[_-]?key\s*[:=]", re.IGNORECASE),
    re.compile(r"password\s*[:=]", re.IGNORECASE),
    re.compile(r"client[_-]?secret\s*[:=]", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}", re.IGNORECASE),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
)


def _find_working_powershell() -> str | None:
    """Return a PowerShell that executes a script block, or ``None``.

    Mirrors ``tests/test_installers.py`` so the skip behaviour is the same
    across the suite.
    """
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if not executable:
            continue
        try:
            probe = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", "Write-Output ok"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "ok" in probe.stdout:
            return executable
    return None


POWERSHELL = _find_working_powershell()
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="no PowerShell on PATH")

WXS_TEXT = WXS.read_text(encoding="utf-8")
ISS_TEXT = ISS.read_text(encoding="utf-8")
SPEC_TEXT = SPEC.read_text(encoding="utf-8")
ENTRY_TEXT = ENTRY.read_text(encoding="utf-8")
BUILD_MSI_TEXT = BUILD_MSI.read_text(encoding="utf-8")
BUILD_EXE_TEXT = BUILD_EXE.read_text(encoding="utf-8")
README_TEXT = README.read_text(encoding="utf-8")

ALL_TEXTS = {
    WXS.name: WXS_TEXT,
    ISS.name: ISS_TEXT,
    SPEC.name: SPEC_TEXT,
    ENTRY.name: ENTRY_TEXT,
    BUILD_MSI.name: BUILD_MSI_TEXT,
    BUILD_EXE.name: BUILD_EXE_TEXT,
    README.name: README_TEXT,
}


# ============================================================= file presence


@pytest.mark.parametrize("path", REQUIRED_FILES, ids=lambda p: p.name)
def test_packaging_file_exists_and_is_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing packaging file: {path}"
    assert path.stat().st_size > 0, f"empty packaging file: {path}"


def test_packaging_dir_holds_only_expected_sources() -> None:
    """No stray build output was committed next to the sources."""
    unexpected = {
        p.name
        for p in PACKAGING_DIR.iterdir()
        if p.is_file() and p.name not in {f.name for f in REQUIRED_FILES}
    }
    assert not unexpected, f"unexpected files in packaging/windows: {sorted(unexpected)}"


# ================================================================= WiX (MSI)


def test_wxs_uses_the_wix_v4_namespace() -> None:
    assert 'xmlns="http://wixtoolset.org/schemas/v4/wxs"' in WXS_TEXT


def test_wxs_declares_the_package_identity() -> None:
    """WiX v4 renamed v3's ``Product`` to ``Package``; the identity lives there."""
    assert re.search(r"<Package\b", WXS_TEXT), "stratum.wxs must author a <Package> element"
    assert 'Name="Stratum-Memory"' in WXS_TEXT
    assert 'Manufacturer="TotalBassJunkie"' in WXS_TEXT
    # winid contract: the winget id shares the Publisher + Product namespace.
    assert 'Scope="perMachine"' in WXS_TEXT


def test_wxs_is_wellformed_xml() -> None:
    """The source must parse as XML before WiX ever sees it."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(WXS_TEXT)
    assert root.tag == "{http://wixtoolset.org/schemas/v4/wxs}Wix"
    # The entry section: exactly one <Package>, plus supporting fragments.
    children = [child.tag.split("}")[-1] for child in root]
    assert children.count("Package") == 1, children
    assert "Fragment" in children, children


def test_wxs_comments_are_xml_legal() -> None:
    """`--` inside a comment is invalid XML and would break `wix build`."""
    for comment in re.finditer(r"<!--(.*?)-->", WXS_TEXT, re.DOTALL):
        assert "--" not in comment.group(1), "XML comments must not contain '--'"


def test_wxs_takes_the_version_from_the_preprocessor() -> None:
    assert 'Version="$(var.Version)"' in WXS_TEXT
    # A build without -d Version must fail instead of shipping a bogus version.
    assert "<?ifndef Version ?>" in WXS_TEXT
    assert "<?error" in WXS_TEXT


def test_wxs_has_a_fixed_documented_upgrade_code() -> None:
    match = re.search(
        r'UpgradeCode="([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}'
        r'-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"',
        WXS_TEXT,
    )
    assert match, "stratum.wxs must set a literal UpgradeCode GUID"
    # The constant is not spelled out inside a variable/define.
    assert "$(" not in match.group(1)
    # ...and it is documented as fixed, not per-build.
    assert "UpgradeCode: FIXED VALUE" in WXS_TEXT


def test_wxs_authors_a_major_upgrade_rule() -> None:
    assert "<MajorUpgrade" in WXS_TEXT
    assert 'AllowDowngrades="no"' in WXS_TEXT
    assert "DowngradeErrorMessage" in WXS_TEXT


def test_wxs_installs_into_program_files_stratum() -> None:
    assert "<StandardDirectory Id=\"ProgramFiles64Folder\">" in WXS_TEXT
    assert 'Directory Id="INSTALLFOLDER" Name="Stratum"' in WXS_TEXT


def test_wxs_installs_the_pyinstaller_executable() -> None:
    assert "<File Id=\"StratumExe\"" in WXS_TEXT
    assert 'Source="$(ExePath)"' in WXS_TEXT


def test_wxs_puts_stratum_on_the_path() -> None:
    assert "<Environment" in WXS_TEXT
    assert 'Name="PATH"' in WXS_TEXT
    assert 'Value="[INSTALLFOLDER]"' in WXS_TEXT
    # machine-wide PATH entry, removed again on uninstall.
    assert 'System="yes"' in WXS_TEXT
    assert 'Permanent="no"' in WXS_TEXT


def test_wxs_creates_a_start_menu_shortcut() -> None:
    assert "<Shortcut" in WXS_TEXT
    assert 'Directory="StratumMenuFolder"' in WXS_TEXT
    assert 'Target="[#StratumExe]"' in WXS_TEXT


def test_wxs_authors_the_arp_entries() -> None:
    assert 'Property Id="ARPPRODUCTICON"' in WXS_TEXT
    assert 'Property Id="ARPCOMMENTS"' in WXS_TEXT
    assert 'Property Id="ARPURLINFOABOUT"' in WXS_TEXT
    assert "<Icon " in WXS_TEXT
    # License note: Apache-2.0, per pyproject.toml.
    assert "Apache-2.0" in WXS_TEXT
    # Optional license page, authored only when -d LicenseRtf is passed.
    assert "WixUILicenseRtf" in WXS_TEXT


def test_wxs_never_hardcodes_a_version() -> None:
    assert not re.search(r'Version="\d+\.\d+', WXS_TEXT), "product version must come from $(var.Version)"


# ============================================================ Inno Setup (EXE)


def test_iss_has_a_setup_section() -> None:
    assert "[Setup]" in ISS_TEXT


def test_iss_declares_the_app_identity() -> None:
    assert "AppName=Stratum-Memory" in ISS_TEXT
    assert "AppVersion={#Version}" in ISS_TEXT
    assert "AppId=" in ISS_TEXT


def test_iss_takes_the_version_from_a_define() -> None:
    assert "#define Version" in ISS_TEXT
    # A compile without /DVersion (or STRATUM_VERSION) must fail, not guess.
    assert "#error" in ISS_TEXT
    assert 'GetEnv("STRATUM_VERSION")' in ISS_TEXT


def test_iss_output_base_filename_is_composed_from_the_version() -> None:
    assert "OutputBaseFilename=Stratum-{#Version}-setup" in ISS_TEXT


def test_iss_uses_autopf_stratum_as_default_dir() -> None:
    assert "DefaultDirName={autopf}\\Stratum" in ISS_TEXT


def test_iss_is_per_user_by_default_with_an_admin_override() -> None:
    assert "PrivilegesRequired=lowest" in ISS_TEXT
    assert "PrivilegesRequiredOverridesAllowed=" in ISS_TEXT
    assert "commandline" in ISS_TEXT


def test_iss_installs_the_pyinstaller_executable() -> None:
    assert "[Files]" in ISS_TEXT
    assert 'DestName: "stratum.exe"' in ISS_TEXT
    assert "{#ExePath}" in ISS_TEXT


def test_iss_updates_the_path_and_broadcasts_the_change() -> None:
    assert "ChangesEnvironment=yes" in ISS_TEXT
    assert "[Registry]" in ISS_TEXT
    assert "NeedsAddPath" in ISS_TEXT


def test_iss_path_entry_never_uses_uninsdeletevalue() -> None:
    """`uninsdeletevalue` on the PATH entry wipes the WHOLE user PATH.

    It deletes the registry *value* (`Path`), not just our own entry, so every
    other directory on the user's PATH would be lost on uninstall.
    """
    for number, line in enumerate(ISS_TEXT.splitlines(), start=1):
        if not line.lstrip().startswith("Root:"):
            continue
        assert 'ValueName: "Path"' in line, f"stratum.iss:{number} is not the PATH entry"
        assert "uninsdeletevalue" not in line, (
            f"stratum.iss:{number} deletes the whole PATH value on uninstall"
        )


def test_iss_uninstall_is_clean() -> None:
    """The uninstaller removes only this app's own PATH entry, by hand."""
    assert "uninsdeletevalue" not in ISS_TEXT, (
        "the PATH entry must be filtered in [Code], not deleted wholesale"
    )
    assert "CurUninstallStepChanged" in ISS_TEXT
    assert "StripPathEntry" in ISS_TEXT, "the PATH entry must be filtered, not blanked"


def test_iss_uninstall_hook_filters_only_the_own_entry() -> None:
    """The hook must compare against {app} and delete the value only if empty."""
    code = ISS_TEXT.split("[Code]", 1)[1]
    hook = code.split("procedure CurUninstallStepChanged", 1)
    assert len(hook) == 2, "the uninstall hook must be defined in [Code]"
    body = hook[1]

    # It runs at uninstall time...
    assert "usUninstall" in body
    # ...reads the existing Path instead of overwriting it blindly...
    assert "RegQueryStringValue" in body
    # ...compares case-insensitively (Windows PATH is) against this app's own
    # directory only - the comparison lives in the helper it calls...
    assert "CompareText" in code, "the entry comparison must be case-insensitive"
    assert "ExpandConstant('{app}')" in body, "only {app} may be removed"
    assert "StripPathEntry" in body, "the hook must filter, not blank the value"
    # ...writes the filtered value back, preserving the string type...
    assert "RegWriteExpandStringValue" in body
    # ...and deletes the value only when nothing else is left.
    assert "RegDeleteValue" in body
    assert "if Updated = '' then" in body, "Path may only be deleted when it is empty"


def test_iss_strip_path_entry_tolerates_missing_entries() -> None:
    """The filter must survive a missing value, empty entries and repeats."""
    code = ISS_TEXT.split("[Code]", 1)[1]
    assert "function StripPathEntry" in code, "the filter helper must exist"
    function = code.split("function StripPathEntry", 1)[1].split("procedure ", 1)[0]
    # Empty entries are dropped (`Current <> ''`) and the loop terminates on the
    # last segment (`if Index = 0 then Break`), so a missing or trailing `;`
    # cannot raise or loop for ever.
    assert "Current <> ''" in function
    assert "Index = 0" in function
    assert "Break" in function


def test_iss_never_hardcodes_a_version() -> None:
    assert "AppVersion={#Version}" in ISS_TEXT
    assert not re.search(r"^AppVersion=\d+\.\d+", ISS_TEXT, re.MULTILINE)


def test_iss_no_constant_appears_inside_a_semicolon_comment() -> None:
    """A `{...}` in a `;` comment is still parsed, so a typo becomes a compile error.

    Neither an ISPP variable nor an Inno constant has any business inside a
    comment, so the whole file keeps them out of every `;` comment.
    """
    for number, line in enumerate(ISS_TEXT.splitlines(), start=1):
        if not line.lstrip().startswith(";"):
            continue
        assert "{#" not in line, f"stratum.iss:{number} expands a variable in a comment"
        for constant in ("{app}", "{autopf}", "{pf}", "{localappdata}", "{group}"):
            assert constant not in line, (
                f"stratum.iss:{number} uses {constant} inside a comment"
            )


def _pascal_comment_violations(code: str) -> list[str]:
    """Find Pascal comments whose body contains a nested ``{``.

    In Inno Setup's ``[Code]`` section ``{ ... }`` is a comment and ``{`` has no
    escape, so a nested brace closes the comment early and the remainder is
    parsed as Pascal - which fails to compile. Single-quoted Pascal strings are
    skipped first, because ``'{app}'`` inside ``ExpandConstant(...)`` is a string
    literal, not a comment.
    """
    without_strings: list[str] = []
    in_string = False
    index = 0
    while index < len(code):
        char = code[index]
        if char == "'":
            if in_string and index + 1 < len(code) and code[index + 1] == "'":
                index += 2  # escaped quote inside a string
                continue
            in_string = not in_string
            without_strings.append(" ")
        elif in_string:
            without_strings.append(" ")
        else:
            without_strings.append(char)
        index += 1

    violations: list[str] = []
    in_comment = False
    start = 0
    for position, char in enumerate(without_strings):
        if char == "{":
            if in_comment:
                violations.append(code[start : position + 1])
            else:
                in_comment = True
                start = position
        elif char == "}":
            in_comment = False
    return violations


def test_iss_pascal_comments_have_no_nested_braces() -> None:
    code_section = ISS_TEXT.split("[Code]", 1)[1]
    violations = _pascal_comment_violations(code_section)
    assert not violations, f"nested brace inside a Pascal comment: {violations}"


def test_pascal_comment_scanner_detects_a_nested_brace() -> None:
    """The guard above is only useful if it actually fires."""
    assert _pascal_comment_violations("{ ok }") == []
    assert _pascal_comment_violations("{ bad {app} }")
    # A constant inside a string literal is fine, not a comment.
    assert _pascal_comment_violations("X := ExpandConstant('{app}');") == []


def test_iss_registry_entry_is_a_single_line() -> None:
    """Inno Setup has no backslash line continuation outside `[Code]`."""
    for line in ISS_TEXT.splitlines():
        assert not line.rstrip().endswith("\\"), f"unexpected line continuation: {line!r}"


# ============================================================ build scripts


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
def test_build_script_uses_strict_error_handling(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert "$ErrorActionPreference = 'Stop'" in text
    assert "Set-StrictMode" in text


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
def test_build_script_takes_a_version_parameter(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert "[string] $Version" in text
    assert "__version__" in text, "the version must be derivable from the package"


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
@needs_powershell
def test_build_script_parses(script: Path) -> None:
    """A syntax error must fail here, not on the windows-latest runner."""
    assert POWERSHELL is not None
    parse = (
        "$ErrorActionPreference = 'Stop'\n"
        "$tokens = $null\n"
        "$errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$tokens, [ref]$errors)\n"
        "if (@($errors).Count -gt 0) {\n"
        "  @($errors) | ForEach-Object { Write-Output $_.Message }\n"
        "  exit 1\n"
        "}\n"
        "exit 0\n"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", parse],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_build_msi_script_installs_wix_and_builds_the_contract_asset() -> None:
    assert "dotnet tool install --global wix" in BUILD_MSI_TEXT
    assert "wix build" in BUILD_MSI_TEXT
    # The version reaches WiX as a preprocessor variable, not as literal text.
    assert '-d "Version=$Version"' in BUILD_MSI_TEXT
    # ...and the artefact carries the contract name.
    assert '"Stratum-$Version-x64.msi"' in BUILD_MSI_TEXT
    assert MSI_ASSET.replace("<version>", "$Version") in BUILD_MSI_TEXT


def test_build_exe_script_installs_inno_and_builds_the_contract_asset() -> None:
    assert "winget install" in BUILD_EXE_TEXT
    assert "choco install innosetup" in BUILD_EXE_TEXT
    assert "ISCC" in BUILD_EXE_TEXT
    # The version reaches the Inno Setup preprocessor via /D.
    assert '"/DVersion=$Version"' in BUILD_EXE_TEXT
    # ...and the artefact carries the contract name.
    assert '"Stratum-$Version-setup.exe"' in BUILD_EXE_TEXT
    assert EXE_ASSET.replace("<version>", "$Version") in BUILD_EXE_TEXT


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
def test_build_script_builds_the_pyinstaller_spec(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert "PyInstaller" in text
    assert "stratum.spec" in text


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
def test_build_script_never_hardcodes_an_asset_version(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert not re.search(r"Stratum-\d+\.\d+", text), "asset names must be composed from -Version"


# ============================================================ PyInstaller spec


def test_spec_freezes_the_stratum_cli_entry_point() -> None:
    assert "stratum.cli" in SPEC_TEXT or "stratum_entry" in SPEC_TEXT
    assert 'name="stratum"' in SPEC_TEXT
    assert "console=True" in SPEC_TEXT


def test_spec_collects_stratum_submodules_and_the_mcp_package() -> None:
    assert "collect_submodules" in SPEC_TEXT
    assert 'collect_submodules("stratum")' in SPEC_TEXT
    # `mcp` is imported lazily, and `stratum.mcp` shadows the package name.
    assert '"mcp"' in SPEC_TEXT


def test_entry_launcher_delegates_to_stratum_cli() -> None:
    assert "from stratum.cli import main" in ENTRY_TEXT
    assert "sys.exit(main())" in ENTRY_TEXT


def test_spec_is_valid_python() -> None:
    compile(SPEC_TEXT, str(SPEC), "exec")


# ==================================================================== README


def test_readme_documents_both_install_commands() -> None:
    assert "msiexec /i" in README_TEXT
    assert "-setup.exe" in README_TEXT


def test_readme_documents_both_build_scripts() -> None:
    assert "build-msi.ps1" in README_TEXT
    assert "build-exe.ps1" in README_TEXT
    assert "-Version" in README_TEXT


# =================================================================== secrets


@pytest.mark.parametrize("name", sorted(ALL_TEXTS), ids=sorted(ALL_TEXTS))
def test_packaging_sources_contain_no_secrets(name: str) -> None:
    text = ALL_TEXTS[name]
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"{name} looks like it carries a secret: {pattern.pattern}"


def test_artefact_names_are_composed_from_the_version_not_frozen() -> None:
    """Guard the naming contract itself: ``Stratum-<version>-…`` in both scripts."""
    for name, text in ((BUILD_MSI.name, BUILD_MSI_TEXT), (BUILD_EXE.name, BUILD_EXE_TEXT)):
        assert "$Version" in text, f"{name} must interpolate the version"
        assert "Stratum-$Version-" in text, f"{name} must compose the contract asset name"
    # A concrete version (the example above) must appear nowhere.
    assert VERSION not in BUILD_MSI_TEXT
    assert VERSION not in BUILD_EXE_TEXT
