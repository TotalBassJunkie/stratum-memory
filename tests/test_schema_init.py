"""DB-free tests for the parameterized schema init (WP-02 / vector-type fix).

The canonical schema no longer hard-codes ``halfvec``. Instead:

* ``init/003_schema.sql.template`` holds the DDL with two placeholders,
  ``__VEC__`` (column type) and ``__VEC_OPS__`` (cosine operator class).
* ``init/003_schema.sh`` runs from ``/docker-entrypoint-initdb.d`` (after
  ``002_app_role.sql``, before ``004_rls.sql``), substitutes the placeholders
  from ``STRATUM_VECTOR_TYPE`` via ``sed`` and pipes the result through ``psql``.

No Postgres or Docker is required here: we inspect the template, emulate the
``sed`` substitution in Python, and syntax-check the shell wrapper when a bash
is available.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_DIR = REPO_ROOT / "init"
TEMPLATE = INIT_DIR / "003_schema.sql.template"
WRAPPER = INIT_DIR / "003_schema.sh"

PLACEHOLDER_VEC = "__VEC__"
PLACEHOLDER_VEC_OPS = "__VEC_OPS__"

# The two supported deployment profiles (must mirror 003_schema.sh and Settings).
PROFILES = {
    "halfvec": ("halfvec", "halfvec_cosine_ops"),
    "vector": ("vector", "vector_cosine_ops"),
}


def _render(text: str, vec_type: str) -> str:
    """Reproduce 003_schema.sh's sed substitution in Python.

    The wrapper does ``s/__VEC_OPS__/<ops>/g`` first, then ``s/__VEC__/<vec>/g``
    (that order matters: ``__VEC__`` is a prefix of ``__VEC_OPS__``).
    """
    vec, vec_ops = PROFILES[vec_type]
    return text.replace(PLACEHOLDER_VEC_OPS, vec_ops).replace(PLACEHOLDER_VEC, vec)


def _ddl(text: str) -> str:
    """Strip ``--`` comment lines so counts reflect executable DDL only."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


# ------------------------------------------------------------- files present


def test_wrapper_exists_and_template_exists():
    assert WRAPPER.is_file(), f"missing init wrapper: {WRAPPER}"
    assert TEMPLATE.is_file(), f"missing schema template: {TEMPLATE}"


def test_no_conflicting_003_sql_file():
    """The old ``003_schema.sql`` must be gone so only the wrapper runs."""
    assert not (INIT_DIR / "003_schema.sql").exists(), (
        "init/003_schema.sql must be removed; it would run as a plain .sql "
        "before/alongside 003_schema.sh and re-introduce the hard-coded type"
    )


def test_init_ordering_no_basename_conflict():
    """Wrapper sorts after 002_app_role.sql and before 004_rls.sql, no clash."""
    names = sorted(p.name for p in INIT_DIR.iterdir() if p.is_file())
    assert "003_schema.sh" in names
    assert names.index("003_schema.sh") > names.index("002_app_role.sql")
    assert names.index("003_schema.sh") < names.index("004_rls.sql")
    # docker-entrypoint-initdb.d runs *.sql and executable *.sh; the .template file
    # is ignored, so no two *executed* init files share a basename/stem.
    executed = [n for n in names if n.endswith(".sql") or n.endswith(".sh")]
    stems = [n.rsplit(".", 1)[0] for n in executed]
    assert stems.count("003_schema") == 1


# ------------------------------------------------------------- template shape


def test_template_has_placeholders():
    body = _ddl(TEMPLATE.read_text(encoding="utf-8"))
    assert body.count(PLACEHOLDER_VEC) == 4, "expected __VEC__ on all 4 embeddings"
    assert body.count(PLACEHOLDER_VEC_OPS) == 3, "expected __VEC_OPS__ on all 3 HNSW indexes"


def test_template_has_no_hardcoded_halfvec():
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "halfvec" not in body, "template still contains a hard-coded 'halfvec'"
    assert "halfvec_cosine_ops" not in body


def test_template_has_no_hardcoded_vector_type():
    """No hard-coded vector type/ops either — only the placeholders."""
    body = TEMPLATE.read_text(encoding="utf-8")
    assert "vector(1024)" not in body
    assert "vector_cosine_ops" not in body


# ------------------------------------------------------------- substitution


def test_render_vector_profile():
    body = TEMPLATE.read_text(encoding="utf-8")
    out = _render(body, "vector")
    assert "__VEC__" not in out and "__VEC_OPS__" not in out
    assert "vector(1024)" in out
    assert "vector_cosine_ops" in out
    assert "halfvec" not in out


def test_render_halfvec_default_profile():
    body = TEMPLATE.read_text(encoding="utf-8")
    out = _render(body, "halfvec")
    assert "__VEC__" not in out and "__VEC_OPS__" not in out
    assert "halfvec(1024)" in out
    assert "halfvec_cosine_ops" in out
    assert "vector(1024)" not in out
    # The rendered ops must never leak as a bare "vector_cosine_ops".
    assert "vector_cosine_ops" not in out


def test_render_vector_emits_exactly_expected_ops():
    out = _render(_ddl(TEMPLATE.read_text(encoding="utf-8")), "vector")
    assert out.count("vector_cosine_ops") == 3
    assert out.count("vector(1024)") == 4


# ------------------------------------------------------------ wrapper content


def test_wrapper_uses_on_error_stop():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "ON_ERROR_STOP=1" in text


def test_wrapper_uses_psql_with_postgres_user_and_db():
    text = WRAPPER.read_text(encoding="utf-8")
    assert 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' in text


def test_wrapper_is_strict_mode():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text


def test_wrapper_defaults_to_halfvec():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "${STRATUM_VECTOR_TYPE:-halfvec}" in text


def test_wrapper_substitutes_both_placeholders():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "s/__VEC_OPS__/" in text
    assert "s/__VEC__/" in text
    # Order matters: __VEC_OPS__ must be substituted before __VEC__.
    assert text.index("s/__VEC_OPS__/") < text.index("s/__VEC__/")


@pytest.mark.skipif(os.name != "nt", reason="POSIX exec bit is implicit")
def test_wrapper_executable_on_windows():
    """On Windows every file reports X_OK; just require the file to be readable."""
    assert os.access(WRAPPER, os.R_OK)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit semantics")
def test_wrapper_executable_on_posix():
    mode = WRAPPER.stat().st_mode
    assert mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), (
        "003_schema.sh must be executable for the postgres init entrypoint"
    )


# --------------------------------------------------------------- bash -n


def _bash() -> str | None:
    if os.name == "nt":
        # On Windows, bash resolves to the WSL shim which needs a real distro.
        # Only trust a bash that can actually report a version.
        candidates = [r"C:\Program Files\Git\bin\bash.exe"]
        found = shutil.which("bash")
        if found:
            candidates.append(found)
        for cand in candidates:
            if cand and Path(cand).exists():
                try:
                    subprocess.run(
                        [cand, "--version"],
                        capture_output=True,
                        timeout=15,
                        check=True,
                    )
                    return cand
                except (OSError, subprocess.SubprocessError):
                    continue
        return None
    return shutil.which("bash")


def test_bash_syntax():
    bash = _bash()
    if not bash:
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, "-n", str(WRAPPER)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"
