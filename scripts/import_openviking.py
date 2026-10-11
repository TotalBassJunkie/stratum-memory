#!/usr/bin/env python3
"""Import OpenViking knowledge into Stratum-Memory.

Runs *on the Pi* against two localhost services (no data leaves the host, no
paid API): OpenViking's REST API (read) and Stratum's own pipeline (write).

Two modes:
  --mode structure   Files + chunks + embeddings only (fast, cheap, fully
                     searchable). No LLM fact extraction.
  --mode ecl         Full Extract-Cognify-Load: additionally runs LLM fact
                     extraction per chunk (slower, more free-model calls).

Resumable: every processed OpenViking URI is recorded in a checkpoint file, so a
re-run skips what is already done. Safe to interrupt (Ctrl-C / reboot).

Usage (on the Pi, inside the repo, with the venv active):
  OPENVIKING_API_KEY_FILE=/run/secrets/ov_user_key \
  python scripts/import_openviking.py --mode ecl --tenant example \
      --areas memories skills resources codex privacy sessions

Config (env):
  OPENVIKING_BASE_URL     default http://127.0.0.1:1933
  OPENVIKING_API_KEY_FILE path to a file holding the USER api key (required)
  OPENVIKING_ACCOUNT      default account
  OPENVIKING_USER         default user
  STRATUM_IMPORT_STATE    checkpoint path (default var/import_openviking.state)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx

# Imported from the installed package (pip install .).
from stratum import db
from stratum.config import Settings
from stratum.filesystem import FilesystemStore
from stratum.graph import TemporalGraph
from stratum.ingest import IngestPipeline
from stratum.llm import LLMClient
from stratum.uri import UriError, parse_uri
from stratum.vector import MemoryStore

# OpenViking areas under viking://user/<user>/ and how we tag them.
DEFAULT_AREAS = ["memories", "skills", "resources", "codex", "privacy", "sessions"]

_VALID_SEG = re.compile(r"^[a-z0-9._~-]+$")


def _sanitize_segment(seg: str) -> str:
    """Map an arbitrary path segment to Stratum's allowed charset.

    Stratum URI segments are lower-case ``[a-z0-9._~-]``. OpenViking paths may
    contain upper-case letters, spaces, ``/`` and Unicode; we transliterate to a
    stable, collision-resistant slug.

    ``str.isalnum()`` is **True for Unicode letters** (e.g. ``ä``, ``漢``), which
    would leak characters past the ASCII-only URI grammar. We therefore
    NFKD-normalise and drop non-ASCII bytes first, so the result is guaranteed to
    match :data:`stratum.uri._SEGMENT_RE`.
    """
    seg = seg.strip()
    # ``encode("ascii", "ignore")`` strips every non-ASCII code point after the
    # NFKD decomposition (which also folds ``ä`` -> ``a`` + combining diaeresis,
    # so the base letter survives).
    ascii_seg = unicodedata.normalize("NFKD", seg).encode("ascii", "ignore").decode("ascii")
    ascii_seg = ascii_seg.lower()
    out = []
    for ch in ascii_seg:
        if ch.isalnum() or ch in "-_~":
            out.append(ch)
        elif ch == ".":
            out.append(".")
        else:
            out.append("-")
    slug = "".join(out)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    if not slug:
        slug = "x"
    return slug[:120]


class UriBuildError(ValueError):
    """Raised when an OpenViking path cannot be mapped to a valid Stratum URI."""


def _target_uri(tenant: str, ov_uri: str) -> str:
    """viking://user/<user>/<rel> -> stratum://docs/<tenant>/openviking/<rel>.

    The rendered URI is validated with :func:`stratum.uri.parse_uri`; a segment
    that survives sanitising but still yields an illegal URI (or a URI longer
    than the 255-char ceiling) raises :class:`UriBuildError` so the caller can
    skip the file instead of importing it under a broken address (B4).
    """
    rel = ov_uri.split("viking://user/", 1)[-1]
    # drop the leading user name segment
    parts = rel.split("/")
    parts = parts[1:] if parts else parts
    parts = [_sanitize_segment(p) for p in parts if p]
    path = "/".join(parts) or "root"
    uri = f"stratum://docs/{tenant}/openviking/{path}"
    try:
        return parse_uri(uri).uri
    except UriError as exc:  # pragma: no cover - defensive; sanitising is strict
        raise UriBuildError(f"cannot map {ov_uri!r} to a valid Stratum URI: {exc}") from exc


class OpenVikingClient:
    def __init__(self, base_url: str, api_key: str, account: str, user: str) -> None:
        self.base = base_url.rstrip("/")
        self.headers = {
            "X-API-Key": api_key,
            "X-OpenViking-Account": account,
            "X-OpenViking-User": user,
        }
        self._client = httpx.Client(timeout=30.0)

    def tree(self, uri: str, depth: int = 20) -> list[dict]:
        r = self._client.get(
            f"{self.base}/api/v1/fs/tree",
            params={"uri": uri, "depth": depth},
            headers=self.headers,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"tree failed: {data.get('error')}")
        return data["result"] or []

    def read(self, uri: str) -> str:
        r = self._client.get(
            f"{self.base}/api/v1/content/read",
            params={"uri": uri},
            headers=self.headers,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"read failed for {uri}: {data.get('error')}")
        return data.get("result") or ""

    def close(self) -> None:
        self._client.close()


def _load_state(path: Path) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text()).get("done", []))
    return set()


def _save_state(path: Path, done: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}))
    tmp.replace(path)


async def _import_one(
    pipeline: IngestPipeline,
    ov: OpenVikingClient,
    *,
    tenant: str,
    ov_uri: str,
    mode: str,
) -> tuple[bool, str]:
    try:
        content = ov.read(ov_uri)
    except Exception as e:  # noqa: BLE001 - report and continue
        return False, f"read-error: {e}"
    if not content.strip():
        return True, "empty"
    try:
        target = _target_uri(tenant, ov_uri)
    except UriBuildError as e:
        # Never import under a broken address: surface it as a hard failure (B4).
        return False, f"bad-uri: {e}"
    ecl = mode == "ecl"
    try:
        res = await pipeline.ingest(
            content,
            tenant=tenant,
            source_uri=target,
            kind="fact" if ecl else "doc",
            skip_facts=not ecl,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"ingest-error: {e}"
    info = f"chunks={res.chunks_new} facts={res.facts_staged} trunc={res.truncated}"
    if getattr(res, "truncated", False):
        # A truncated run did NOT import the whole file: do not checkpoint it as
        # done, or a re-run would skip the remainder forever (B5).
        return False, f"truncated ({info})"
    return True, info


async def run(args: argparse.Namespace) -> int:
    key_file = os.environ.get("OPENVIKING_API_KEY_FILE")
    if not key_file or not Path(key_file).is_file():
        print("ERROR: set OPENVIKING_API_KEY_FILE to a readable file with the user key", file=sys.stderr)
        return 2
    key = Path(key_file).read_text().strip()
    base = os.environ.get("OPENVIKING_BASE_URL", "http://127.0.0.1:1933")
    account = os.environ.get("OPENVIKING_ACCOUNT", "account")
    user = os.environ.get("OPENVIKING_USER", "user")

    # Ensure the target tenant row exists (chunks/facts FK -> tenants).
    try:
        with db.connection(bypass_rls=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s, %s) "
                "ON CONFLICT (tenant_id) DO NOTHING",
                (args.tenant, args.tenant),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        # Previously a silent WARN: the import then failed later on an FK error.
        # If the tenant cannot be created every chunk/fact insert is doomed, so
        # abort loudly with a non-zero exit instead of importing nothing (B3).
        print(
            f"ERROR: could not ensure tenant {args.tenant!r}: {e}\n"
            "       (grants: `GRANT SELECT, INSERT ON tenants TO stratum_app;` — "
            "see init/004_rls.sql)",
            file=sys.stderr,
        )
        return 2

    settings = Settings()
    llm = LLMClient(settings)
    store = MemoryStore(settings)
    fs = FilesystemStore(settings)
    graph = TemporalGraph(settings)
    pipeline = IngestPipeline(settings, llm=llm, store=store, fs=fs, graph=graph)

    state_path = Path(os.environ.get("STRATUM_IMPORT_STATE", "var/import_openviking.state"))
    done = _load_state(state_path)
    print(f"checkpoint: {state_path} ({len(done)} already done)")

    ov = OpenVikingClient(base, key, account, user)
    total = ok = skipped = failed = 0
    t0 = time.time()
    try:
        for area in args.areas:
            root = f"viking://user/{user}/{area}"
            try:
                entries = ov.tree(root)
            except Exception as e:  # noqa: BLE001
                print(f"[{area}] tree error: {e}", file=sys.stderr)
                continue
            files = [e for e in entries if not e.get("isDir")]
            print(f"[{area}] {len(files)} files")
            for e in files:
                uri = e.get("uri")
                total += 1
                # An entry without a URI (or a non-``viking://`` one) cannot be
                # read or mapped: skip it loudly rather than raise on ``e["uri"]``
                # or feed a junk URI to the importer (B13).
                if not isinstance(uri, str) or not uri.startswith("viking://"):
                    failed += 1
                    print(f"FAIL <no uri>: entry without a viking:// uri: {e!r}", file=sys.stderr)
                    continue
                if uri in done:
                    skipped += 1
                    continue
                good, info = await _import_one(
                    pipeline, ov, tenant=args.tenant, ov_uri=uri, mode=args.mode
                )
                if good:
                    ok += 1
                    done.add(uri)
                    if ok % 25 == 0:
                        _save_state(state_path, done)
                else:
                    failed += 1
                    # A failure used to be counted but never shown: the ``info``
                    # (error/timeout/truncation) must reach the operator (B5).
                    print(f"FAIL {uri}: {info}", file=sys.stderr)
                # Gentle pacing: the chat/embed providers may rate-limit. A tiny
                # sleep keeps a multi-thousand-file bulk run under the limit.
                if args.sleep > 0:
                    await asyncio.sleep(args.sleep)
                if total % 25 == 0:
                    rate = total / max(1e-6, time.time() - t0)
                    print(
                        f"  progress: {total} seen, {ok} ok, {failed} failed, "
                        f"{skipped} skipped ({rate:.1f}/s)"
                    )
                if args.limit and ok >= args.limit:
                    print("limit reached; stopping")
                    break
            if args.limit and ok >= args.limit:
                break
    finally:
        ov.close()
        _save_state(state_path, done)

    print(
        f"DONE: total={total} ok={ok} failed={failed} skipped={skipped} "
        f"in {time.time() - t0:.0f}s"
    )
    # Non-zero exit when anything failed, so a wrapper/cron notices (B13).
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import OpenViking knowledge into Stratum.")
    ap.add_argument("--mode", choices=["structure", "ecl"], default="structure")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--areas", nargs="+", default=DEFAULT_AREAS)
    ap.add_argument("--limit", type=int, default=0, help="stop after N successful imports (0=all)")
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to sleep after each file (throttle under provider rate limits)",
    )
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
