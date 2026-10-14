"""Tests for the npm wrapper package (``npm/``).

The npm channel is documented in ``docs/distribution.md`` as
``npm install -g stratum-memory``. What npm ships is deliberately **only a
wrapper**: the real command line tool is the Python CLI (``stratum``), so the
shim must *locate* it, never re-implement it and never install anything behind
the user's back.

Pinned here are the properties that make that wrapper safe:

* ``npm/package.json`` is valid, carries the required metadata and its version
  matches ``stratum.__version__`` (the single version source, per
  ``docs/distribution.md``),
* ``npm/bin/stratum.js`` is a syntactically valid Node script with a shebang,
* the shim cannot recurse into itself (it skips its own npm bin wrapper) and it
  fails with a clear message plus exit code 3 when no Python CLI is present,
* the published package contains no secrets.

The suite is **DB-free, network-free and side-effect-free**: it reads files,
pattern-matches them, runs ``node --check`` and - when Node is available -
executes the shim once against a private, shim-only ``PATH`` in a temp
directory. Nothing is installed, nothing is downloaded.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import stratum

REPO_ROOT = Path(__file__).resolve().parents[1]
NPM_DIR = REPO_ROOT / "npm"
PACKAGE_JSON = NPM_DIR / "package.json"
SHIM = NPM_DIR / "bin" / "stratum.js"
NPM_README = NPM_DIR / "README.md"
NPMIGNORE = NPM_DIR / ".npmignore"

#: Exit code the shim uses when no Python CLI could be found (see npm/README.md).
EXIT_NO_PYTHON = 3

#: Metadata npm requires for a publishable package.
REQUIRED_FIELDS = (
    "name",
    "version",
    "description",
    "license",
    "bin",
    "engines",
    "files",
    "repository",
    "homepage",
    "bugs",
    "keywords",
)

#: Install channels the shim's failure message must point at.
INSTALL_HINTS = (
    "pipx install stratum-memory",
    "pip install",
    "get.stratum.dev/install.sh",
    "install.ps1",
    "winget",
    "msi",
    "Docker",
)

#: Anything that looks like a leaked credential must not appear in the package.
SECRET_PATTERNS = (
    r"ghp_[A-Za-z0-9]{16,}",
    r"github_pat_[A-Za-z0-9_]{16,}",
    r"npm_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']",
)

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="no node on PATH")

PACKAGE_TEXT = PACKAGE_JSON.read_text(encoding="utf-8")
SHIM_TEXT = SHIM.read_text(encoding="utf-8")
README_TEXT = NPM_README.read_text(encoding="utf-8")


# ============================================================== text utilities


def _strip_js_comments(text: str) -> str:
    """Drop ``//`` and ``/* ... */`` comments, leaving only executable code.

    The shim legitimately *mentions* a ``stratum`` wrapper and a raw
    ``stratum``-shaped call in its comments (explaining what it must not do);
    those must not count as real code when the recursion guard is asserted.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(re.sub(r"//.*$", "", line) for line in text.splitlines())


def _js_function_body(code: str, name: str) -> str:
    """Return the body of top-level ``function <name>(...) { ... }``."""
    match = re.search(rf"^function {re.escape(name)}\(", code, flags=re.MULTILINE)
    assert match, f"function {name}() not found in the shim"
    end = code.find("\n}", match.end())
    assert end != -1, f"could not find the end of function {name}()"
    return code[match.start() : end + 2]


def _spawn_call_arguments(code: str) -> list[str]:
    """The parenthesised argument text of every process-spawning call."""
    regions: list[str] = []
    for match in re.finditer(r"\b(?:spawnSync|spawnCommand|execSync|execFileSync)\s*\(", code):
        index = match.end()
        depth = 1
        while index < len(code) and depth > 0:
            char = code[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        regions.append(code[match.end() : index - 1])
    return regions


SHIM_CODE = _strip_js_comments(SHIM_TEXT)


def _npm_files() -> list[Path]:
    return sorted(path for path in NPM_DIR.rglob("*") if path.is_file())


# ============================================================ package.json


def test_package_json_is_valid_json() -> None:
    data = json.loads(PACKAGE_TEXT)
    assert isinstance(data, dict)


@pytest.fixture(scope="module")
def package() -> dict:
    return json.loads(PACKAGE_TEXT)


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_package_json_has_required_field(package: dict, field: str) -> None:
    assert field in package, f"npm/package.json is missing '{field}'"
    assert package[field] not in ("", None, [], {}), f"'{field}' is empty"


def test_package_name_is_stratum_memory(package: dict) -> None:
    assert package["name"] == "stratum-memory"


def test_package_version_matches_python_package(package: dict) -> None:
    """``docs/distribution.md``: the version source is ``stratum.__version__``."""
    assert package["version"] == stratum.__version__
    assert package["version"] == "0.1.0"


def test_package_license_is_apache_2(package: dict) -> None:
    assert package["license"] == "Apache-2.0"


def test_package_bin_points_at_the_shim(package: dict) -> None:
    assert package["bin"] == {"stratum": "bin/stratum.js"}


def test_package_engines_require_node_18(package: dict) -> None:
    assert package["engines"] == {"node": ">=18"}


def test_package_files_whitelist(package: dict) -> None:
    assert package["files"] == ["bin/", "README.md"]


def test_package_repository_and_homepage(package: dict) -> None:
    assert "TotalBassJunkie/stratum-memory" in package["repository"]
    assert "TotalBassJunkie/stratum-memory" in package["homepage"]
    assert "TotalBassJunkie/stratum-memory" in package["bugs"]["url"]


def test_package_keywords_are_present(package: dict) -> None:
    assert isinstance(package["keywords"], list)
    assert "stratum" in package["keywords"]
    assert "mcp" in package["keywords"]


def test_package_has_no_lifecycle_scripts(package: dict) -> None:
    """No hidden install magic: the wrapper must never install Python itself."""
    scripts = package.get("scripts", {})
    assert scripts == {}, f"unexpected npm lifecycle scripts: {sorted(scripts)}"


# ================================================================ the shim


def test_shim_exists_and_has_node_shebang() -> None:
    assert SHIM.is_file(), f"missing shim: {SHIM}"
    first_line = SHIM_TEXT.splitlines()[0]
    assert first_line == "#!/usr/bin/env node", first_line


@needs_node
def test_shim_is_syntactically_valid() -> None:
    result = subprocess.run(
        [NODE, "--check", str(SHIM)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_shim_resolves_the_cli_via_python_module_fallbacks() -> None:
    assert '"python3"' in SHIM_CODE
    assert '"python"' in SHIM_CODE
    assert '"py"' in SHIM_CODE
    assert '"stratum.cli"' in SHIM_CODE
    # The Python >= 3.12 floor of the whole project is probed, not assumed.
    assert "(3, 12)" in SHIM_CODE


def test_shim_never_calls_a_literal_stratum_command() -> None:
    """A literal ``stratum`` call would be a blind self-invocation.

    Only *process-spawning* calls count: the failure message legitimately names
    ``stratum install`` and friends as commands the user should run.
    """
    for arguments in _spawn_call_arguments(SHIM_CODE):
        assert not re.search(r"[\"'`][^\"'`]*\bstratum\b", arguments), arguments
        assert "stratum" not in arguments.replace("stratum.cli", "").replace(
            "stratum-memory", ""
        ), arguments


def test_the_blind_call_detector_actually_detects() -> None:
    """Guard against a vacuous recursion check (the scanner must be sharp)."""
    synthetic = 'spawnSync("stratum", args, { stdio: "inherit" });'
    regions = _spawn_call_arguments(synthetic)
    assert regions, "the spawn scanner found no call in a known-blind snippet"
    assert "stratum" in regions[0]


def test_shim_path_resolution_never_executes_anything() -> None:
    """The PATH scan is pure: it may only *look* at candidates, never run them."""
    body = _js_function_body(SHIM_CODE, "findStratumOnPath")
    for banned in ("spawnSync", "spawnCommand", "execSync", "execFileSync", "spawn("):
        assert banned not in body, f"findStratumOnPath() must not {banned}"


def test_shim_skips_its_own_wrapper() -> None:
    """Self-recursion is prevented by an explicit guard around every candidate."""
    body = _js_function_body(SHIM_CODE, "findStratumOnPath")
    assert "isSelf(" in body, "findStratumOnPath() must check candidates against itself"
    assert "continue" in body, "the self-check must skip the candidate (continue)"
    assert "function isSelf(" in SHIM_CODE
    # The guard compares against this very file, not against a hard-coded name.
    assert "realpathSync(__filename)" in SHIM_CODE or "__filename" in SHIM_CODE


def test_shim_fails_with_exit_code_3_and_install_hints() -> None:
    assert re.search(r"EXIT_NO_PYTHON\s*=\s*3\b", SHIM_CODE)
    assert re.search(r"return\s+EXIT_NO_PYTHON\s*;", SHIM_CODE)
    assert re.search(r"process\.exit\(main\(", SHIM_CODE)


@pytest.mark.parametrize("hint", INSTALL_HINTS)
def test_shim_failure_message_mentions_every_install_channel(hint: str) -> None:
    assert hint in SHIM_TEXT, f"install guidance is missing {hint!r}"


def test_shim_does_not_install_anything_itself() -> None:
    """The guidance is *printed*; it must never be executed by the shim.

    ``printMissingCli()`` is the only place that mentions pip/pipx/curl/winget,
    and it contains no process-spawning call at all.
    """
    body = _js_function_body(SHIM_CODE, "printMissingCli")
    assert "process.stderr.write" in body, "printMissingCli() must only print"
    for banned in ("spawnSync", "spawnCommand", "execSync", "execFileSync", "spawn(", "child_process"):
        assert banned not in body, f"printMissingCli() must not use {banned}"
    # ... and the shim spawns nothing outside of the two explicit helpers.
    for arguments in _spawn_call_arguments(SHIM_CODE):
        assert "pip " not in arguments and "npm " not in arguments
        assert "curl" not in arguments and "winget" not in arguments
    assert "postinstall" not in PACKAGE_TEXT


# ============================================================== dynamic check


def _fake_npm_wrapper(directory: Path, marker: Path) -> Path:
    """Write npm's *own* bin wrapper for this package into *directory*.

    npm generates a ``stratum`` (POSIX) / ``stratum.cmd`` (Windows) shim next to
    the real script that points back at ``bin/stratum.js``. Re-running it would
    recurse forever, so the real shim must recognise and skip it. The marker
    file records the catastrophe: it is written *only* when the wrapper runs.
    """
    name = "stratum.cmd" if os.name == "nt" else "stratum"
    body = '@echo off\r\necho RECURSED>"%MARKER%"\r\nexit /b 0\r\n' if os.name == "nt" else '#!/bin/sh\necho RECURSED > "$MARKER"\nexit 0\n'
    wrapper = directory / name
    # Both shapes reference the target by path, exactly like npm's real wrapper.
    header = "# npm wrapper for stratum-memory/bin/stratum.js\n"
    wrapper.write_text(header + body, encoding="utf-8", newline="")
    wrapper.chmod(0o755)
    return wrapper


@needs_node
def test_shim_does_not_recurse_and_exits_3_without_python(tmp_path: Path) -> None:
    """End-to-end: private PATH, no Python, our own npm wrapper on PATH.

    Correct behaviour is: recognise the wrapper as *ourselves*, skip it, find no
    Python, print the guidance and exit 3. A blind self-invocation would either
    loop forever or run the marker-writing wrapper.
    """
    fake_bin = tmp_path / "npm-bin"
    fake_bin.mkdir()
    marker = tmp_path / "recursed.txt"
    _fake_npm_wrapper(fake_bin, marker)

    # Only the fake npm bin is reachable, so neither a real `stratum` nor a
    # `python`/`py` can leak in. SystemRoot/ComSpec stay in place because Node
    # and cmd.exe need them.
    environment = {**os.environ, "PATH": str(fake_bin), "MARKER": str(marker)}
    result = subprocess.run(
        [NODE, str(SHIM), "version"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=environment,
    )

    assert not marker.exists(), "the shim executed its own npm wrapper (self-recursion)"
    assert result.returncode == EXIT_NO_PYTHON, result.stdout + result.stderr
    assert "not found" in result.stderr
    assert "pipx install stratum-memory" in result.stderr
    assert "Docker" in result.stderr


@needs_node
def test_shim_forwards_arguments_to_the_found_cli(tmp_path: Path) -> None:
    """With a (fake) ``stratum`` on PATH the shim is a transparent pass-through.

    The stand-in is a plain wrapper that is *not* this shim (no reference to
    ``bin/stratum.js``), so it must be executed with the arguments and its exit
    code must be propagated.
    """
    fake_bin = tmp_path / "path-bin"
    fake_bin.mkdir()
    recorder = tmp_path / "args.txt"
    if os.name == "nt":
        fake = fake_bin / "stratum.cmd"
        fake.write_text(
            '@echo off\r\necho %* > "%RECORDER%"\r\nexit /b 7\r\n',
            encoding="utf-8",
            newline="",
        )
    else:
        fake = fake_bin / "stratum"
        fake.write_text(
            '#!/bin/sh\necho "$*" > "$RECORDER"\nexit 7\n',
            encoding="utf-8",
            newline="\n",
        )
    fake.chmod(0o755)

    environment = {**os.environ, "PATH": str(fake_bin), "RECORDER": str(recorder)}
    result = subprocess.run(
        [NODE, str(SHIM), "doctor", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=environment,
    )

    assert recorder.is_file(), "the CLI on PATH was not executed"
    recorded = recorder.read_text(encoding="utf-8")
    assert "doctor" in recorded, recorded
    assert "--json" in recorded, recorded
    assert result.returncode == 7, "the CLI's exit code must be propagated"


# ============================================================ docs and hygiene


def test_npmignore_excludes_tests_and_node_modules() -> None:
    assert NPMIGNORE.is_file(), "npm/.npmignore is missing"
    text = NPMIGNORE.read_text(encoding="utf-8")
    assert "tests/" in text
    assert "node_modules/" in text


def test_readme_covers_the_wrapper_contract() -> None:
    lowered = README_TEXT.lower()
    for os_name in ("windows", "macos", "linux"):
        assert os_name in lowered, f"npm/README.md does not mention {os_name}"
    assert "wrapper" in lowered, "the README must state that this is a Python wrapper"
    assert "python" in lowered
    assert "docker" in lowered, "the README must mention the Docker requirement"
    assert "exit code 3" in lowered or "exit 3" in lowered
    assert "npm install -g stratum-memory" in README_TEXT
    assert "npx stratum-memory" in README_TEXT


def test_package_contains_no_secrets() -> None:
    offenders: list[str] = []
    for path in _npm_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(REPO_ROOT)} matches {pattern}")
    assert not offenders, offenders


def test_npm_directory_contains_only_expected_files() -> None:
    expected = {
        Path("npm/package.json"),
        Path("npm/README.md"),
        Path("npm/.npmignore"),
        Path("npm/bin/stratum.js"),
    }
    found = {path.relative_to(REPO_ROOT) for path in _npm_files()}
    assert found == expected, f"unexpected npm files: {sorted(found ^ expected)}"
