"""Tests for the GitHub Actions workflows (``.github/workflows/``).

``docs/distribution.md`` is the contract: on a ``v<version>`` tag GitHub Actions
builds every installer named in its "Release assets" table and attaches them to
the GitHub Release. The workflows in ``.github/workflows/`` are the *only*
implementation of that promise, so the contract is pinned here as executable
expectations.

The suite is deliberately **database-free, network-free and side-effect-free**:
it only reads the YAML/scripts, parses them (PyYAML; skipped when it is absent)
and pattern-matches them. Nothing is built, uploaded or published, and no secret
is read.

What is pinned:

* every workflow file exists and parses as YAML,
* ``release.yml`` triggers on ``v*`` (and can be dispatched with a ``version``
  input),
* it references every existing build source *literally* (``nfpm.yaml``,
  ``build-msi.ps1``, ``build-exe.ps1``, ``build-pkg.sh``, ``build-dmg.sh``,
  ``build-appimage.sh``, ``Dockerfile``),
* it contains every contract asset name, ``SHA256SUMS`` included,
* the ``release`` job needs every build job, generates ``SHA256SUMS`` and
  rewrites the manifest placeholders,
* the mandatory channels (GitHub Release, GHCR) work with ``GITHUB_TOKEN``,
  while every optional publish channel is secret-gated,
* no workflow uses ``pull_request_target`` and no credential is hard-coded.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from stratum import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

RELEASE = WORKFLOW_DIR / "release.yml"
CI = WORKFLOW_DIR / "ci.yml"
PUBLISH_PYPI = WORKFLOW_DIR / "publish-pypi.yml"
PUBLISH_NPM = WORKFLOW_DIR / "publish-npm.yml"
PUBLISH_CONTAINER = WORKFLOW_DIR / "publish-container.yml"

#: Every workflow this ticket owns.
WORKFLOWS = (RELEASE, CI, PUBLISH_PYPI, PUBLISH_NPM, PUBLISH_CONTAINER)

#: The build sources the release workflow must call, exactly as they exist.
BUILD_SOURCES = (
    "packaging/linux/nfpm.yaml",
    "packaging/windows/build-msi.ps1",
    "packaging/windows/build-exe.ps1",
    "packaging/macos/build-pkg.sh",
    "packaging/macos/build-dmg.sh",
    "packaging/linux/build-appimage.sh",
    "Dockerfile",
)

#: The asset names fixed by docs/distribution.md, with ``<version>`` standing in
#: for the CI-provided ``${VERSION}`` / ``${{ ... }}`` expression. The two Python
#: distributions carry the PEP 503-normalised project name (``stratum_memory``),
#: not the PyPI name's dash spelling - see ``python_distribution_names()``.
CONTRACT_ASSETS = (
    "stratum_memory-<version>-py3-none-any.whl",
    "stratum_memory-<version>.tar.gz",
    "stratum_<version>_amd64.deb",
    "stratum-<version>-1.x86_64.rpm",
    "Stratum-<version>-x86_64.AppImage",
    "stratum_<version>_amd64.snap",
    "Stratum-<version>-x64.msi",
    "Stratum-<version>-setup.exe",
    "Stratum-<version>-x86_64.pkg",
    "Stratum-<version>-x86_64.dmg",
    "SHA256SUMS",
)

#: Jobs that build assets; the `release` job must depend on all of them.
BUILD_JOBS = ("version", "python-dist", "linux-packages", "windows-installers", "macos-installers")

#: Optional publish jobs and the secret each one is gated on.
PUBLISH_JOBS = {
    "publish-pypi": "PYPI_TOKEN",
    "publish-npm": "NPM_TOKEN",
    "publish-homebrew": "TAP_GITHUB_TOKEN",
    "publish-scoop": "TAP_GITHUB_TOKEN",
    "publish-winget": "WINGET_TOKEN",
    "publish-chocolatey": "CHOCOLATEY_API_KEY",
}

#: Markers of a committed credential (mirrors tests/test_packaging_manifests.py).
SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"npm_[A-Za-z0-9]{20,}"),
    re.compile(r"pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_\-]+"),
)

#: The nuspec XML namespace, for reading the Chocolatey `<file>` globs.
NUSPEC_NS = {"n": "http://schemas.microsoft.com/packaging/2015/06/nuspec.xsd"}

try:  # PyYAML is a dev extra; the YAML tests skip when it is absent.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    yaml = None  # type: ignore[assignment]

needs_yaml = pytest.mark.skipif(yaml is None, reason="PyYAML is not installed")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load(path: Path) -> dict:
    """Parse a workflow file; skips (never fails) without PyYAML."""
    assert yaml is not None
    data = yaml.safe_load(_text(path))
    assert isinstance(data, dict), f"{path.name} must parse to a mapping"
    return data


# ``yaml.safe_load`` reads the bare key ``on`` as the boolean ``True`` (YAML 1.1),
# so every helper below has to accept both spellings.
def _on(workflow: dict) -> dict:
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "workflow must declare its triggers"
    return triggers


def _jobs(workflow: dict) -> dict:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "workflow must declare jobs"
    return jobs


def _job(workflow: dict, name: str) -> dict:
    job = _jobs(workflow).get(name)
    assert isinstance(job, dict), f"workflow has no job {name!r}"
    return job


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    assert isinstance(steps, list), "job must declare steps"
    return steps


def _run_text(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


# ============================================================== file presence


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_exists_and_is_nonempty(path: Path) -> None:
    assert path.is_file(), f"missing workflow: {path}"
    assert path.stat().st_size > 0, f"empty workflow: {path}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
@needs_yaml
def test_workflow_parses_as_yaml(path: Path) -> None:
    workflow = _load(path)
    assert workflow["name"], f"{path.name} must carry a name"
    _jobs(workflow)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_sets_minimal_permissions(path: Path) -> None:
    """Every workflow states its permissions explicitly (least privilege)."""
    text = _text(path)
    assert re.search(r"(?m)^permissions:", text), f"{path.name} must set top-level permissions"
    workflow = _load(path)
    assert workflow["permissions"], f"{path.name} must not leave permissions empty"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_uses_the_pinned_actions(path: Path) -> None:
    """Only the action versions the ticket names (or GH's own) are used."""
    text = _text(path)
    for action in re.findall(r"uses:\s*([^\s#]+)", text):
        assert re.match(
            r"^(actions/(checkout@v4|setup-python@v5|setup-node@v4"
            r"|upload-artifact@v4|download-artifact@v4)"
            r"|softprops/action-gh-release@v2"
            r"|pypa/gh-action-pypi-publish@release/v1"
            r"|docker/login-action@v3)$",
            action,
        ), f"{path.name} uses an unexpected action: {action}"


# =========================================================== release triggers


@needs_yaml
def test_release_triggers_on_a_version_tag() -> None:
    triggers = _on(_load(RELEASE))
    push = triggers.get("push")
    assert isinstance(push, dict), "release.yml must trigger on push"
    assert "v*" in push["tags"], "release.yml must trigger on `v*` tags"


@needs_yaml
def test_release_can_be_dispatched_with_a_version_input() -> None:
    triggers = _on(_load(RELEASE))
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict), "release.yml must support workflow_dispatch"
    assert "version" in dispatch["inputs"], "the dispatch input must be named `version`"


def test_release_reads_the_version_from_the_package() -> None:
    text = _text(RELEASE)
    assert "src/stratum/__init__.py" in text, "the version source must be named literally"
    assert "__version__" in text, "the version must come from __version__"


def test_release_checks_the_version_against_the_tag() -> None:
    job = _job(_load(RELEASE), "version")
    assert "version" in job["outputs"], "the version job must publish the version output"
    text = _run_text(job)
    assert "GITHUB_REF_NAME" in text, "the tag name must be checked against the version"
    assert "sys.exit" in text, "a mismatch must stop the run"


# ============================================================ build coverage


@pytest.mark.parametrize("source", BUILD_SOURCES)
def test_release_references_every_build_source(source: str) -> None:
    text = _text(RELEASE)
    assert source in text, f"release.yml must call {source}"
    assert (REPO_ROOT / source).is_file(), f"{source} does not exist in the repository"


def test_release_builds_all_four_platform_jobs() -> None:
    text = _text(RELEASE)
    for job in ("python-dist", "linux-packages", "windows-installers", "macos-installers"):
        assert re.search(rf"(?m)^  {job}:", text), f"release.yml is missing the {job} job"


def test_release_builds_the_wheel_and_the_sdist() -> None:
    job = _job(_load(RELEASE), "python-dist")
    text = _run_text(job)
    assert "python -m build" in text, "python-dist must run `python -m build`"
    assert ".whl" in text and ".tar.gz" in text, "both distributions must be checked"


# ================================================ python-dist file names (BLOCKER)
#
# `python -m build` names the wheel/sdist after the *PEP 503 normalised* project
# name: `name = "stratum-memory"` in pyproject.toml becomes the file prefix
# `stratum_memory`. The first `v0.1.0` release run failed in three jobs because
# the workflow, the Homebrew formula, the AppImage/Snap build scripts, the
# manifest rewriter and the docs all spelled the files `stratum-<version>-...`,
# a name no build ever produces. These tests pin the real names and forbid the
# old spelling from coming back.

#: The literal file prefix `python -m build` emits for this project.
PYTHON_DIST_PREFIX = "stratum_memory"


def python_distribution_names(version: str) -> tuple[str, str]:
    """The wheel and sdist file names ``python -m build`` produces for *version*.

    Derived from the project name the way the PEP 503/PEP 427 file-name rule
    does (lowercase, runs of ``-``/``_``/``.`` collapse to ``_``), so the test
    agrees with a real build instead of hard-coding a spelling. A real build was
    measured on this repository:

        Successfully built stratum_memory-0.1.0.tar.gz
        and stratum_memory-0.1.0-py3-none-any.whl
    """
    project_name = _project_name()
    prefix = re.sub(r"[-_.]+", "_", project_name).lower()
    return (
        f"{prefix}-{version}-py3-none-any.whl",
        f"{prefix}-{version}.tar.gz",
    )


def _project_name() -> str:
    """Read ``[project].name`` from pyproject.toml (no TOML lib needed)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml must declare [project].name"
    return match.group(1)


def test_python_dist_prefix_is_the_normalised_project_name() -> None:
    """The prefix used everywhere must be the build's normalised project name."""
    assert PYTHON_DIST_PREFIX == re.sub(r"[-_.]+", "_", _project_name()).lower()
    assert PYTHON_DIST_PREFIX == "stratum_memory", (
        "the build normalises `stratum-memory` to `stratum_memory`; the workflows, "
        "manifests and build scripts must use that prefix"
    )


def test_release_checks_the_real_python_dist_names() -> None:
    """``python-dist`` must `test -f` the names a real build produces."""
    job = _job(_load(RELEASE), "python-dist")
    text = _run_text(job)
    wheel, sdist = python_distribution_names("${VERSION}")
    assert wheel in text, f"python-dist must check for {wheel}"
    assert sdist in text, f"python-dist must check for {sdist}"


def test_release_forbids_the_old_unbuildable_python_dist_name() -> None:
    """The old `stratum-<version>-py3-none-any.whl` spelling must never return.

    It is not a name `python -m build` can emit (the dash before the version
    makes it look like a second project-name separator), so any `test -f` on it
    fails the release for ever.
    """
    text = _text(RELEASE)
    assert "stratum-${VERSION}-py3-none-any.whl" not in text, (
        "release.yml still expects the unbuildable `stratum-${VERSION}-...whl` name"
    )
    assert "stratum-${VERSION}.tar.gz" not in text, (
        "release.yml still expects the unbuildable `stratum-${VERSION}.tar.gz` name"
    )
    # ...and the release job's asset-presence list must use the real names too.
    assert 'f"stratum_memory-{version}-py3-none-any.whl"' in text
    assert 'f"stratum_memory-{version}.tar.gz"' in text


@pytest.mark.parametrize(
    "path",
    (PUBLISH_PYPI,),
    ids=lambda p: p.name,
)
def test_publish_workflows_check_the_real_python_dist_names(path: Path) -> None:
    """The manual PyPI workflow must check the same real file names."""
    text = _text(path)
    wheel, sdist = python_distribution_names("${VERSION}")
    assert wheel in text, f"{path.name} must check for {wheel}"
    assert sdist in text, f"{path.name} must check for {sdist}"
    assert "stratum-${VERSION}-py3-none-any.whl" not in text
    assert "stratum-${VERSION}.tar.gz" not in text


def test_manifest_rewriter_uses_the_real_wheel_name() -> None:
    """The Homebrew URL the rewriter builds must name the real wheel."""
    body = _text(WORKFLOW_DIR / "scripts" / "update-manifests.py")
    assert 'f"stratum_memory-{version}-py3-none-any.whl"' in body, (
        "update-manifests.py must build the Homebrew URL from the real wheel name"
    )
    assert 'f"stratum-{version}-py3-none-any.whl"' not in body


@pytest.mark.parametrize(
    "path",
    (
        REPO_ROOT / "packaging" / "homebrew" / "stratum-memory.rb",
        REPO_ROOT / "packaging" / "linux" / "build-appimage.sh",
        REPO_ROOT / "packaging" / "linux" / "snapcraft.yaml",
    ),
    ids=lambda p: p.name,
)
def test_packaging_sources_use_the_real_wheel_name(path: Path) -> None:
    """Every consumer of the built wheel must use the normalised name."""
    text = _text(path)
    assert PYTHON_DIST_PREFIX in text, f"{path.name} must reference the {PYTHON_DIST_PREFIX} wheel"
    assert not re.search(r"stratum-(?:<version>|\$\{VERSION\}|\*|0\.1\.0)-?[^\"'\s]*\.whl", text), (
        f"{path.name} still uses the unbuildable `stratum-...whl` spelling"
    )


def test_contract_documents_the_real_python_dist_names() -> None:
    """docs/distribution.md is the contract and must carry the real names."""
    text = _text(REPO_ROOT / "docs" / "distribution.md")
    wheel, sdist = python_distribution_names("<version>")
    assert wheel in text, f"the contract must name {wheel}"
    assert sdist in text, f"the contract must name {sdist}"
    assert "stratum-<version>-py3-none-any.whl" not in text, (
        "the contract still documents the unbuildable wheel name"
    )
    assert "stratum-<version>.tar.gz" not in text, (
        "the contract still documents the unbuildable sdist name"
    )


def test_release_linux_job_builds_deb_rpm_appimage_and_snap() -> None:
    job = _job(_load(RELEASE), "linux-packages")
    text = _run_text(job)
    assert "build-deb-rpm.sh" in text, "the deb/rpm script must be called"
    assert "nfpm" in text, "nFPM must be installed on the runner"
    assert "build-appimage.sh" in text, "the AppImage script must be called"
    assert "snapcraft" in text, "the snap must be built with snapcraft"


def test_release_snap_is_optional_and_renamed_to_the_contract_name() -> None:
    """The snap needs store credentials, so it must not block the release."""
    job = _job(_load(RELEASE), "linux-packages")
    snap_steps = [step for step in _steps(job) if "snapcraft" in str(step.get("run", ""))]
    assert snap_steps, "no step builds the snap"
    assert any(step.get("continue-on-error") for step in snap_steps), (
        "the snap step must be continue-on-error (it needs SNAPCRAFT_STORE_CREDENTIALS)"
    )
    text = "\n".join(str(step.get("run", "")) for step in snap_steps)
    assert "stratum_${VERSION}_amd64.snap" in text, (
        "snapcraft's own file name must be renamed to the contract name"
    )


def test_release_windows_job_calls_both_scripts_with_the_version() -> None:
    job = _job(_load(RELEASE), "windows-installers")
    assert job["runs-on"].startswith("windows"), "the Windows installers need a Windows runner"
    text = _run_text(job)
    assert "build-msi.ps1" in text and "-Version" in text, "the MSI script needs -Version"
    assert "build-exe.ps1" in text, "the EXE script must be called"
    assert "PyInstaller" in text or "pyinstaller" in text, "PyInstaller must be installed"


def test_release_does_not_pin_wix_in_the_workflow() -> None:
    """WiX is installed and invoked inside build-msi.ps1, never in the workflow.

    WiX v7 refuses to run until the OSMF EULA is accepted (``WIX7015``), so the
    version pin or the `-acceptEula wix7` switch belongs to the script that owns
    the `wix` invocation. Pinning a WiX version in release.yml as well would put
    the same decision in two places and drift; the workflow only calls the script.
    """
    # The parsed YAML drops comments, so this only sees *executable* workflow
    # text - the documentation above may mention `wix build`/`acceptEula` freely.
    workflow = _load(RELEASE)
    executable = "\n".join(_run_text(job) for job in _jobs(workflow).values())
    for banned in ("dotnet tool install", "wix build", "wix.exe", "acceptEula", "-acceptEula"):
        assert banned not in executable, (
            f"release.yml must not run WiX directly ({banned!r}); build-msi.ps1 owns it"
        )
    # The OSMF constraint must stay documented next to the call, so the next
    # person does not re-add a hard-coded WiX version here.
    text = _text(RELEASE)
    assert "OSMF" in text and "WIX7015" in text, (
        "release.yml must document why WiX is not pinned in the workflow"
    )


def test_release_macos_job_exports_the_version() -> None:
    job = _job(_load(RELEASE), "macos-installers")
    assert job["runs-on"].startswith("macos"), "the macOS installers need a macOS runner"
    text = _run_text(job)
    assert "build-pkg.sh" in text and "build-dmg.sh" in text, "both macOS scripts must be called"
    env = json.dumps(job, default=str)
    assert "VERSION" in env, "the macOS scripts require VERSION in the environment"


def test_release_container_job_pushes_to_ghcr() -> None:
    job = _job(_load(RELEASE), "container")
    assert job["permissions"].get("packages") == "write", "the container job needs packages: write"
    text = json.dumps(job, default=str)
    assert "ghcr.io/totalbassjunkie/stratum-memory" in text, "the contract image must be used"
    assert "docker push" in text and ":latest" in text, "both :<version> and :latest must be pushed"
    assert "secrets.GITHUB_TOKEN" in text, "the default token must authenticate the push"
    assert "docker/login-action@v3" in text, "the job must log in to the registry"


# ============================================================ release assets


@pytest.mark.parametrize("asset", CONTRACT_ASSETS)
def test_release_names_every_contract_asset(asset: str) -> None:
    """Each asset name is asserted in a `${VERSION}`-templated form."""
    # The job that produces an asset names it exactly; the release job lists the
    # artifacts it attaches, so `*.ext` is accepted as the second spelling.
    stem = asset.replace("<version>", "${VERSION}")
    suffix = stem.rsplit(".", 1)[-1]
    text = _text(RELEASE)
    assert stem in text or f"*.{suffix}" in text, f"release.yml never names {asset}"


def test_release_creates_sha256sums() -> None:
    job = _job(_load(RELEASE), "release")
    text = _run_text(job)
    assert "sha256sum" in text, "the release job must run sha256sum"
    assert "SHA256SUMS" in text, "the checksum file must be named SHA256SUMS"


def test_release_rewrites_the_manifest_placeholders() -> None:
    job = _job(_load(RELEASE), "release")
    text = _run_text(job)
    assert "update-manifests.py" in text, "the manifest rewriter must be called"
    script = WORKFLOW_DIR / "scripts" / "update-manifests.py"
    assert script.is_file(), f"missing manifest rewriter: {script}"
    body = _text(script)
    for manifest in (
        "packaging/homebrew/stratum-memory.rb",
        "packaging/scoop/stratum-memory.json",
        "packaging/winget",
        "packaging/chocolatey",
    ):
        assert manifest.split("/")[-1] in body, f"the rewriter must handle {manifest}"
    assert "sha256" in body.lower(), "the rewriter must substitute checksums"
    # The Chocolatey script moved into tools/ (the nuspec globs tools\**).
    assert 'tools" / "chocolateyinstall.ps1' in body, (
        "the rewriter must write packaging/chocolatey/tools/chocolateyinstall.ps1"
    )
    # The stale-URL guard must run before the tree is uploaded.
    assert "ReleaseNotesUrl" in body, "the rewriter must refresh the winget release notes URL"


def test_release_attaches_the_assets_to_the_github_release() -> None:
    job = _job(_load(RELEASE), "release")
    uses = [step.get("uses") for step in _steps(job)]
    assert "softprops/action-gh-release@v2" in uses, "the release must be created with gh-release"
    assert "actions/download-artifact@v4" in uses, "every build artifact must be collected"
    assert job["permissions"]["contents"] == "write", (
        "creating a release needs contents: write"
    )


@needs_yaml
def test_release_job_needs_every_build_job() -> None:
    needs = set(_needs(_job(_load(RELEASE), "release")))
    missing = set(BUILD_JOBS) - needs
    assert not missing, f"the release job must depend on every build job; missing {sorted(missing)}"


# ==================================================== manifest hand-off (BLOCKER)
#
# The rewriter runs in the `release` job's workspace only. Every publish job does
# a fresh `actions/checkout`, so it would read the committed placeholders again
# (version 0.1.0, sha256 000...0) and `brew`/`scoop`/`choco` would fail on the
# checksum for ever. The rewritten `packaging/` tree therefore has to cross the
# job boundary as an artifact.

#: Artifact that carries the rewritten packaging tree to the publish jobs.
MANIFEST_ARTIFACT = "packaging-manifests"

#: The publish jobs that consume a manifest from `packaging/`.
MANIFEST_PUBLISH_JOBS = (
    "publish-homebrew",
    "publish-scoop",
    "publish-winget",
    "publish-chocolatey",
)

#: The manifest each publish job hands to the outside world.
PUBLISHED_MANIFESTS = {
    "publish-homebrew": "packaging/homebrew/stratum-memory.rb",
    "publish-scoop": "packaging/scoop/stratum-memory.json",
    "publish-winget": "packaging/winget/",
    "publish-chocolatey": "packaging/chocolatey/",
}


def _upload_step(job: dict, name: str) -> dict:
    for step in _steps(job):
        if step.get("uses") == "actions/upload-artifact@v4" and step.get("with", {}).get("name") == name:
            return step
    raise AssertionError(f"no upload-artifact step for {name!r}")


def _download_steps(job: dict, name: str) -> list[dict]:
    return [
        step
        for step in _steps(job)
        if step.get("uses") == "actions/download-artifact@v4"
        and step.get("with", {}).get("name") == name
    ]


def test_release_uploads_the_rewritten_packaging_tree() -> None:
    job = _job(_load(RELEASE), "release")
    step = _upload_step(job, MANIFEST_ARTIFACT)
    assert step["with"]["path"] == "packaging", (
        "the artifact must carry the packaging/ tree the rewriter changed"
    )
    assert step["with"]["if-no-files-found"] == "error"
    # ...and it must be uploaded AFTER the rewrite, or it ships placeholders.
    order = [str(s.get("name", "")) for s in _steps(job)]
    rewrite = next(index for index, name in enumerate(order) if "Rewrite the packaging" in name)
    upload = next(index for index, name in enumerate(order) if name == step["name"])
    assert rewrite < upload, "the artifact must be uploaded after update-manifests.py runs"


@pytest.mark.parametrize("job_name", MANIFEST_PUBLISH_JOBS)
@needs_yaml
def test_publish_jobs_download_the_rewritten_manifests(job_name: str) -> None:
    job = _job(_load(RELEASE), job_name)
    steps = _download_steps(job, MANIFEST_ARTIFACT)
    assert steps, f"{job_name} must download the {MANIFEST_ARTIFACT} artifact"
    assert steps[0]["with"]["path"] == "packaging", (
        f"{job_name} must restore the artifact into packaging/"
    )
    # The download has to come before anything reads or packs a manifest.
    download = next(
        index
        for index, step in enumerate(_steps(job))
        if step.get("uses") == "actions/download-artifact@v4"
        and step.get("with", {}).get("name") == MANIFEST_ARTIFACT
    )
    manifest = PUBLISHED_MANIFESTS[job_name]
    for index, step in enumerate(_steps(job)):
        body = str(step.get("run", "")) + str(step.get("with", ""))
        if manifest in body:
            assert download < index, (
                f"{job_name} touches {manifest} before downloading the rewritten manifests"
            )


@pytest.mark.parametrize("job_name", MANIFEST_PUBLISH_JOBS)
@needs_yaml
def test_publish_jobs_publish_a_real_manifest(job_name: str) -> None:
    """Each job must move the rewritten file into the store, not skip it."""
    text = json.dumps(_job(_load(RELEASE), job_name), default=str)
    assert "packaging/" in text, f"{job_name} must read the manifest from packaging/"
    assert any(
        command in text
        for command in ("cp ", "choco pack", "wingetcreate", "git add")
    ), f"{job_name} never publishes anything"


def test_publish_winget_verifies_the_version_before_submitting() -> None:
    """A placeholder manifest would open a winget PR for the wrong version."""
    job = _job(_load(RELEASE), "publish-winget")
    checks = [
        step
        for step in _steps(job)
        if step.get("if") == "${{ env.WINGET_TOKEN != '' }}"
        and "PackageVersion" in str(step.get("run", ""))
    ]
    assert checks, "publish-winget must check PackageVersion before wingetcreate runs"
    order = [str(step.get("name", "")) for step in _steps(job)]
    check_index = order.index(checks[0]["name"])
    submit_index = next(index for index, name in enumerate(order) if name == "Submit the manifest update")
    assert check_index < submit_index, "the version check must run before the submission"


def test_publish_chocolatey_pack_command_resolves_to_the_install_script() -> None:
    """`choco pack` must name a nuspec whose glob really matches the script.

    `choco pack` copies only what `<file src=...>` matches, so a nuspec globbing
    `tools\\**` while the script sits flat packs a package with no install
    script at all - and the release job would push that empty package.
    """
    job = _job(_load(RELEASE), "publish-chocolatey")
    match = re.search(r"choco pack (\S+\.nuspec)", _run_text(job))
    assert match, "the Chocolatey job must run `choco pack <nuspec>`"

    nuspec = REPO_ROOT / match.group(1)
    assert nuspec.is_file(), f"the pack command names a missing nuspec: {match.group(1)}"

    root = ET.fromstring(nuspec.read_text(encoding="utf-8"))
    sources = [element.get("src") for element in root.findall("n:files/n:file", NUSPEC_NS)]
    assert sources, "the nuspec must declare at least one <file>"
    for source in sources:
        # `tools\**` -> the tools directory, resolved next to the nuspec.
        resolved = (nuspec.parent / source.replace("\\", "/")).parent
        assert resolved.is_dir(), f"the nuspec glob {source!r} matches nothing at {resolved}"
        assert list(resolved.iterdir()), f"the nuspec glob {source!r} matches an empty directory"


def test_manifest_handoff_is_documented_in_the_workflow() -> None:
    """The workflow itself must explain why the artifact exists."""
    text = _text(RELEASE)
    assert MANIFEST_ARTIFACT in text
    assert "fresh `actions/checkout`" in text or "fresh checkout" in text, (
        "the comment must name the reason (fresh checkout loses the rewrite)"
    )


def _guard_snippet() -> str:
    """The inline python of release.yml's placeholder guard."""
    job = _job(_load(RELEASE), "release")
    for step in _steps(job):
        run = str(step.get("run", ""))
        if "placeholder survived the rewrite" in str(step.get("name", "")):
            body = run.split("<<'PY'", 1)[1].split("\nPY", 1)[0]
            return body
    raise AssertionError("release.yml must guard against surviving placeholders")


def _run_guard(tmp_path: Path, packaging: Path, version: str) -> subprocess.CompletedProcess:
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    shutil.copytree(packaging, work / "packaging")
    return subprocess.run(
        [sys.executable, "-c", _guard_snippet()],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(work),
        env={"PATH": "/usr/bin:/bin", "VERSION": version},
    )


def test_placeholder_guard_accepts_a_rewritten_tree(tmp_path: Path) -> None:
    """The guard must not fire on a correctly rewritten packaging tree."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _run_rewriter(scratch, version="0.2.0")

    result = _run_guard(tmp_path, scratch / "packaging", "0.2.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0.2.0" in result.stdout


def test_placeholder_guard_rejects_a_stale_tree(tmp_path: Path) -> None:
    """A tree that still carries 0.1.0 / zeroed digests must stop the release."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _run_rewriter(scratch, version="0.2.0")
    locale = scratch / "packaging" / "winget" / "TotalBassJunkie.StratumMemory.locale.en-US.yaml"
    locale.write_text(
        "PackageVersion: 0.1.0\n"
        "ReleaseNotesUrl: https://github.com/TotalBassJunkie/stratum-memory/releases/tag/v0.1.0\n",
        encoding="utf-8",
    )
    result = _run_guard(tmp_path, scratch / "packaging", "0.2.0")
    assert result.returncode != 0, "a stale ReleaseNotesUrl must fail the guard"
    assert "v0.1.0" in result.stdout + result.stderr

    # ...and the zeroed checksum placeholder is caught as well.
    installer = scratch / "packaging" / "winget" / "TotalBassJunkie.StratumMemory.installer.yaml"
    installer.write_text(
        'InstallerSha256: "' + "0" * 64 + '"\n', encoding="utf-8"
    )
    result = _run_guard(tmp_path, scratch / "packaging", "0.2.0")
    assert result.returncode != 0, "a zeroed checksum must fail the guard"
    assert "zeroed checksum" in result.stdout + result.stderr


# ============================================================ publish channels


@needs_yaml
def test_publish_jobs_are_secret_gated() -> None:
    workflow = _load(RELEASE)
    for name, secret in PUBLISH_JOBS.items():
        job = _job(workflow, name)
        text = json.dumps(job, default=str)
        assert f"secrets.{secret}" in text, f"{name} must be gated on the {secret} secret"
        # The `secrets` context cannot be read in an `if:`, so the secret is
        # lifted into a job-level env var and the condition tests that.
        assert job.get("env", {}).get(secret), f"{name} must lift {secret} into a job env var"
        conditions = [str(step["if"]) for step in _steps(job) if "if" in step]
        assert conditions, f"{name} must conditionally run its publish steps"
        assert all(f"secrets.{secret}" not in condition for condition in conditions), (
            f"{name} must not read secrets.{secret} in an if: condition"
        )
        assert any(f"env.{secret}" in condition for condition in conditions), (
            f"{name} must gate on env.{secret}"
        )


@needs_yaml
def test_optional_channels_never_block_the_release() -> None:
    workflow = _load(RELEASE)
    for name in PUBLISH_JOBS:
        needs = set(_needs(_job(workflow, name)))
        assert "release" in needs, f"{name} runs after the GitHub Release"
    # ...and the mandatory release job does not wait for any optional channel.
    assert not (set(PUBLISH_JOBS) & set(_needs(_job(workflow, "release"))))


def test_pypi_prefers_trusted_publishing() -> None:
    job = _job(_load(RELEASE), "publish-pypi")
    assert job["permissions"].get("id-token") == "write", "trusted publishing needs id-token: write"
    assert "pypa/gh-action-pypi-publish@release/v1" in json.dumps(job), (
        "the official publish action must be used"
    )


def test_winget_uses_wingetcreate() -> None:
    text = json.dumps(_job(_load(RELEASE), "publish-winget"), default=str)
    assert "wingetcreate" in text, "winget manifests are submitted with wingetcreate"


# ================================================================ other workflows


@needs_yaml
def test_ci_runs_on_pull_requests_and_main() -> None:
    triggers = _on(_load(CI))
    push = triggers.get("push")
    assert isinstance(push, dict), "ci.yml must trigger on push"
    assert "main" in push["branches"], "ci.yml must run on main"
    assert "pull_request" in triggers, "ci.yml must run on pull requests"


def test_ci_runs_ruff_pytest_and_the_installer_checks() -> None:
    text = _text(CI)
    assert "ruff" in text, "ci.yml must run ruff"
    assert 'pytest -m "not integration"' in text, "ci.yml must run the DB-free suite"
    assert "bash -n install.sh" in text, "ci.yml must syntax-check install.sh"
    assert "install.ps1" in text and "Parser" in text, (
        "ci.yml must parse install.ps1 with the PowerShell language parser"
    )


def test_ci_builds_the_docker_image_without_pushing() -> None:
    text = _text(CI)
    assert "docker build" in text, "ci.yml must build the image as a smoke test"
    assert "docker push" not in text, "CI must never push an image"


@pytest.mark.parametrize("path", (PUBLISH_PYPI, PUBLISH_NPM, PUBLISH_CONTAINER), ids=lambda p: p.name)
@needs_yaml
def test_publish_workflows_are_manual(path: Path) -> None:
    triggers = _on(_load(path))
    assert "workflow_dispatch" in triggers, f"{path.name} must be manually dispatchable"
    assert "push" not in triggers, f"{path.name} must not run on push"
    assert "pull_request" not in triggers, f"{path.name} must not run on pull requests"


def test_publish_pypi_uses_oidc_and_the_token_fallback() -> None:
    text = _text(PUBLISH_PYPI)
    assert "id-token: write" in text, "trusted publishing needs id-token: write"
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "secrets.PYPI_TOKEN" in text, "the token fallback must stay documented"


def test_publish_npm_uses_the_npm_token() -> None:
    text = _text(PUBLISH_NPM)
    assert "secrets.NPM_TOKEN" in text
    assert "NODE_AUTH_TOKEN" in text, "npm needs the token in NODE_AUTH_TOKEN"
    assert "npm publish" in text


def test_publish_container_pushes_to_ghcr_with_the_default_token() -> None:
    job = _job(_load(PUBLISH_CONTAINER), "publish")
    assert job["permissions"].get("packages") == "write"
    text = _text(PUBLISH_CONTAINER)
    assert "ghcr.io/totalbassjunkie/stratum-memory" in text
    assert "secrets.GITHUB_TOKEN" in text, "the default token authenticates GHCR"
    assert "docker push" in text


# ==================================================================== safety


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_pull_request_target(path: Path) -> None:
    assert "pull_request_target" not in _text(path), (
        f"{path.name} must not use pull_request_target (untrusted code + secrets)"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_hardcoded_credentials(path: Path) -> None:
    text = _text(path)
    for pattern in SECRET_PATTERNS:
        match = pattern.search(text)
        assert match is None, f"{path.name} looks like it carries a secret: {match.group(0)!r}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_secrets_are_only_referenced_through_the_secrets_context(path: Path) -> None:
    """A secret value may only enter through ``${{ secrets.* }}``."""
    text = _text(path)
    for name in re.findall(r"(?m)^\s*([A-Z][A-Z0-9_]{3,})\s*:\s*(.+)$", text):
        variable, value = name
        if not any(token in variable for token in ("TOKEN", "SECRET", "KEY", "PASSWORD")):
            continue
        assert "secrets." in value or value.startswith("${{"), (
            f"{path.name} sets {variable} to something other than a secret expression"
        )


def test_version_source_matches_the_package() -> None:
    """The workflow's version check must agree with the shipped ``__version__``."""
    init = (REPO_ROOT / "src" / "stratum" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, flags=re.MULTILINE)
    assert match, "src/stratum/__init__.py must define __version__"
    assert match.group(1) == __version__, "the tests and the package disagree about the version"


# ================================================ the manifest rewriter itself


def _run_rewriter(tmp_path: Path, version: str = "9.9.9") -> tuple[int, str]:
    """Run the rewriter against a copy of the real manifests and assets."""
    packaging = tmp_path / "packaging"
    shutil.copytree(REPO_ROOT / "packaging", packaging)
    dist = tmp_path / "dist"
    dist.mkdir()
    assets = (
        f"stratum_memory-{version}-py3-none-any.whl",
        f"Stratum-{version}-setup.exe",
        f"Stratum-{version}-x64.msi",
    )
    lines = []
    for index, asset in enumerate(assets):
        digest = f"{index + 1:064x}"
        (dist / asset).write_text("stub", encoding="utf-8")
        lines.append(f"{digest}  {asset}")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(WORKFLOW_DIR / "scripts" / "update-manifests.py")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={
            "PATH": "/usr/bin:/bin",
            "VERSION": version,
            "GITHUB_REPOSITORY": "TotalBassJunkie/stratum-memory",
            "DIST_DIR": str(dist),
            "PACKAGING_DIR": str(packaging),
        },
    )
    return result.returncode, result.stdout + result.stderr


def test_manifest_rewriter_replaces_every_placeholder(tmp_path: Path) -> None:
    code, output = _run_rewriter(tmp_path)
    assert code == 0, output

    homebrew = (tmp_path / "packaging" / "homebrew" / "stratum-memory.rb").read_text(
        encoding="utf-8"
    )
    assert "9.9.9" in homebrew, "the homebrew URL must carry the new version"
    assert f"{1:064x}" in homebrew, "the homebrew sha256 must be the wheel's digest"

    scoop = json.loads(
        (tmp_path / "packaging" / "scoop" / "stratum-memory.json").read_text(encoding="utf-8")
    )
    assert scoop["version"] == "9.9.9"
    assert scoop["architecture"]["64bit"]["url"].endswith("Stratum-9.9.9-setup.exe")
    assert scoop["architecture"]["64bit"]["hash"] == f"{2:064x}"

    installer = (tmp_path / "packaging" / "winget" / "TotalBassJunkie.StratumMemory.installer.yaml").read_text(
        encoding="utf-8"
    )
    assert "PackageVersion: 9.9.9" in installer
    assert "Stratum-9.9.9-x64.msi" in installer
    assert f"{3:064x}" in installer

    nuspec = (tmp_path / "packaging" / "chocolatey" / "stratum-memory.nuspec").read_text(
        encoding="utf-8"
    )
    assert "<version>9.9.9</version>" in nuspec

    install = (
        tmp_path / "packaging" / "chocolatey" / "tools" / "chocolateyinstall.ps1"
    ).read_text(encoding="utf-8")
    assert "Stratum-9.9.9-x64.msi" in install
    assert f"{3:064x}" in install


def test_manifest_rewriter_updates_the_winget_release_notes_url(tmp_path: Path) -> None:
    """A v0.2.0 release must not keep advertising the v0.1.0 release notes."""
    code, output = _run_rewriter(tmp_path, version="0.2.0")
    assert code == 0, output

    locale = (
        tmp_path / "packaging" / "winget" / "TotalBassJunkie.StratumMemory.locale.en-US.yaml"
    ).read_text(encoding="utf-8")
    assert "PackageVersion: 0.2.0" in locale
    assert (
        "ReleaseNotesUrl: https://github.com/TotalBassJunkie/stratum-memory/releases/tag/v0.2.0"
        in locale
    ), "ReleaseNotesUrl must follow the new version"
    assert "releases/tag/v0.1.0" not in locale, "the stale v0.1.0 URL must be gone"


def test_manifest_rewriter_leaves_no_placeholder_behind(tmp_path: Path) -> None:
    """Whatever the publish jobs read must be free of 0.1.0 and zeroed digests."""
    code, output = _run_rewriter(tmp_path, version="0.2.0")
    assert code == 0, output

    scanned = [
        path
        for path in sorted((tmp_path / "packaging").rglob("*"))
        if path.is_file() and path.suffix in {".rb", ".json", ".yaml", ".nuspec", ".ps1"}
    ]
    assert scanned, "the rewriter must have touched manifests"
    zero = "0" * 64
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        assert zero not in text, f"{path.name} still carries the zeroed checksum"
        assert not re.search(r"releases/(?:download|tag)/v(?!0\.2\.0)[0-9][^/\"'\s]*", text), (
            f"{path.name} still points at a stale tag"
        )


def test_manifest_rewriter_fails_loudly_without_checksums(tmp_path: Path) -> None:
    packaging = tmp_path / "packaging"
    shutil.copytree(REPO_ROOT / "packaging", packaging)
    dist = tmp_path / "dist"
    dist.mkdir()
    result = subprocess.run(
        [sys.executable, str(WORKFLOW_DIR / "scripts" / "update-manifests.py")],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={
            "PATH": "/usr/bin:/bin",
            "VERSION": "9.9.9",
            "GITHUB_REPOSITORY": "TotalBassJunkie/stratum-memory",
            "DIST_DIR": str(dist),
            "PACKAGING_DIR": str(packaging),
        },
    )
    assert result.returncode != 0, "a missing SHA256SUMS must not pass silently"
    assert "SHA256SUMS" in result.stdout + result.stderr
