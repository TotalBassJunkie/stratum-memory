"""Reference implementation of the Stratum-Memory backup retention rule (WP-15).

The backup tooling (:file:`backup.sh`, :file:`backup.ps1`) writes two kinds of
archive per run, each carrying a UTC timestamp in its name:

* ``stratum-YYYYmmdd-HHMMSS.dump`` — ``pg_dump --format=custom`` archive of the
  ``stratum`` database,
* ``vault-YYYYmmdd-HHMMSS.tar.gz`` — tarball of the Git vault (``kb``).

Because the timestamp is fixed-width and lexicographically sortable, retention
("keep only the newest *N*") is a pure *name* operation — no mtime, no
filesystem, no database. This module is the normative description of that rule;
:file:`backup.sh` mirrors it with ``sort -r`` and an index walk,
:file:`backup.ps1` with ``Sort-Object Name -Descending``.

Names without a parseable ``YYYYmmdd-HHMMSS`` stamp are **never** pruned, so
foreign files that happen to live in the backup directory (README, logs, manual
exports) survive. The module is standard-library only and therefore trivially
unit-testable without Postgres or Docker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

#: ``strftime``/``strptime`` pattern of the embedded timestamp.
TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Matches the timestamp part of an archive name.
TIMESTAMP_RE = re.compile(r"(\d{8}-\d{6})")

#: Canonical database-dump file name (``stratum-YYYYmmdd-HHMMSS.dump``).
DUMP_NAME_RE = re.compile(r"^stratum-\d{8}-\d{6}\.dump$")

#: Canonical vault-archive file name (``vault-YYYYmmdd-HHMMSS.tar.gz``).
VAULT_NAME_RE = re.compile(r"^vault-\d{8}-\d{6}\.tar\.gz$")


def backup_timestamp(name: str) -> datetime | None:
    """Return the UTC timestamp embedded in *name*, or ``None``.

    The first ``YYYYmmdd-HHMMSS`` occurrence that is a *valid* calendar date is
    used; a syntactically matching but impossible stamp (``20261332-250000``)
    is treated like a missing one, so such a file is never deleted.

    Args:
        name: File name (or path) of a backup archive.

    Returns:
        The parsed :class:`datetime.datetime`, or ``None`` when *name* carries
        no usable stamp.
    """
    match = TIMESTAMP_RE.search(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), TIMESTAMP_FORMAT)
    except ValueError:
        return None


def select_backups_to_prune(names: Iterable[str], keep: int) -> list[str]:
    """Return the archive names to delete so that only the newest *keep* remain.

    The selection is name-based: archives are ranked by their embedded
    timestamp, the newest *keep* are kept and the rest is returned. Names
    without a usable stamp are ignored (never pruned). Duplicate input names
    are de-duplicated.

    Args:
        names: Candidate archive names (a directory listing, typically).
        keep: Number of newest archives to retain per call. ``0`` prunes every
            stamped archive; negative values are rejected.

    Returns:
        The names to delete, ordered oldest-first for a deterministic,
        readable plan.

    Raises:
        TypeError: If *keep* is not an ``int``.
        ValueError: If *keep* is negative.
    """
    if isinstance(keep, bool) or not isinstance(keep, int):
        raise TypeError("keep must be an int")
    if keep < 0:
        raise ValueError("keep must be >= 0")

    stamped: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        timestamp = backup_timestamp(name)
        if timestamp is None:
            continue
        stamped.append((timestamp, name))

    # Newest first; the string tie-breaker keeps equal stamps deterministic.
    stamped.sort(key=lambda item: (item[0], item[1]), reverse=True)
    leftovers = stamped[keep:]
    # Oldest first for the returned plan.
    leftovers.sort(key=lambda item: (item[0], item[1]))
    return [name for _, name in leftovers]
