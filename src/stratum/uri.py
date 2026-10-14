"""Unified URI namespace: stratum://<bereich>/<tenant>/<pfad>."""

from __future__ import annotations

import re
from dataclasses import dataclass

BEREICHE = ("docs", "facts", "entities", "episodes", "skills", "syntheses")
_SCHEME = "stratum"
_SEGMENT_RE = re.compile(r"^[a-z0-9._~-]+$")
_MAX_LEN = 255


class UriError(ValueError):
    """Raised for malformed stratum:// URIs."""


@dataclass(frozen=True)
class StratumUri:
    bereich: str
    tenant: str
    path: str

    @property
    def uri(self) -> str:
        return f"{_SCHEME}://{self.bereich}/{self.tenant}/{self.path}"

    def with_path(self, new_path: str) -> StratumUri:
        return StratumUri(self.bereich, self.tenant, new_path)


def parse_uri(uri: str) -> StratumUri:
    if not uri.startswith(f"{_SCHEME}://"):
        raise UriError(f"URI must start with '{_SCHEME}://': {uri!r}")
    if len(uri) > _MAX_LEN:
        raise UriError(f"URI exceeds {_MAX_LEN} chars: {uri!r}")
    rest = uri[len(_SCHEME) + 3 :]
    if ".." in rest.split("/") or "." in rest.split("/"):
        raise UriError(f"URI must not contain '.' or '..' segments: {uri!r}")
    parts = rest.split("/")
    if len(parts) < 3:
        raise UriError(
            f"URI must be stratum://<bereich>/<tenant>/<pfad>, got: {uri!r}"
        )
    bereich, tenant, *path_parts = parts
    path = "/".join(path_parts)
    if bereich not in BEREICHE:
        raise UriError(f"Unknown bereich {bereich!r}; allowed: {BEREICHE}")
    for seg in (tenant, *path_parts):
        if not seg or not _SEGMENT_RE.match(seg):
            raise UriError(
                f"Invalid segment {seg!r}; allowed chars [a-z0-9._~-] and non-empty"
            )
    return StratumUri(bereich=bereich, tenant=tenant, path=path)


def build_uri(bereich: str, tenant: str, path: str) -> str:
    return parse_uri(f"{_SCHEME}://{bereich}/{tenant}/{path}").uri
