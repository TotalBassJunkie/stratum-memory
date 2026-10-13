"""Import a local Markdown vault into Stratum-Memory.

Unlike ``import_openviking.py`` (which reads OpenViking's REST API), this walks a
directory tree of ``*.md`` files on disk and pushes each through the ECL
pipeline. Generic — works for any Markdown vault.

Runs on the Pi against the local Stratum stack. Resumable via a checkpoint file.
Cost-free with a local/free embedding backend.

Usage (inside the core container or a compose ``run`` with the vault mounted):
  python scripts/import_vault.py /path/to/vault --tenant example \
      --mode structure --sleep 0

Env: same as import_openviking.py for Stratum (STRATUM_*).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from stratum import db
from stratum.config import Settings
from stratum.filesystem import FilesystemStore
from stratum.graph import TemporalGraph
from stratum.ingest import IngestPipeline
from stratum.llm import LLMClient
from stratum.vector import MemoryStore


def _sanitize_segment(seg: str) -> str:
    import unicodedata

    seg = unicodedata.normalize("NFKD", seg).encode("ascii", "ignore").decode("ascii")
    seg = seg.strip().lower()
    out = []
    for ch in seg:
        if ch.isalnum() or ch in "-_~":
            out.append(ch)
        elif ch == ".":
            out.append(".")
        else:
            out.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(out)).strip("-.")
    return (slug or "x")[:120]


def _target_uri(tenant: str, rel: Path) -> str:
    parts = [_sanitize_segment(p) for p in rel.with_suffix("").parts if p]
    # Cap total length under the 255-char URI limit.
    path = "/".join(parts) or "root"
    prefix = f"stratum://docs/{tenant}/vault/"
    if len(prefix) + len(path) > 250:
        digest = hashlib.sha1(path.encode()).hexdigest()[:12]
        path = f"{path[:200]}-{digest}"
    return prefix + path


def _load_state(path: Path) -> set[str]:
    if path.exists():
        return set(json.loads(path.read_text()).get("done", []))
    return set()


def _save_state(path: Path, done: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": sorted(done)}))
    tmp.replace(path)


async def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    # Ensure tenant exists.
    try:
        with db.connection(bypass_rls=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (tenant_id, name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (args.tenant, args.tenant),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"WARN: could not ensure tenant: {e}", file=sys.stderr)

    settings = Settings()
    fs = FilesystemStore(settings)
    pipeline = IngestPipeline(
        settings,
        llm=LLMClient(settings),
        store=MemoryStore(settings),
        fs=fs,
        graph=TemporalGraph(settings),
    )

    state = Path(os.environ.get("STRATUM_IMPORT_STATE", "/app/var/import_vault.state"))
    done = _load_state(state)
    print(f"checkpoint: {state} ({len(done)} already done)")

    files = sorted(root.rglob("*.md"))
    print(f"[vault] {len(files)} markdown files under {root}")

    ok = failed = skipped = seen = 0
    t0 = time.time()
    for f in files:
        seen += 1
        key = str(f)
        if key in done:
            skipped += 1
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            failed += 1
            print(f"FAIL(read) {f}: {e}", file=sys.stderr)
            continue
        if not content.strip():
            skipped += 1
            done.add(key)
            continue
        rel = f.relative_to(root)
        uri = _target_uri(args.tenant, rel)
        try:
            res = await pipeline.ingest(
                content,
                tenant=args.tenant,
                source_uri=uri,
                kind="fact" if args.mode == "ecl" else "doc",
                skip_facts=args.mode != "ecl",
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL(ingest) {f}: {e}", file=sys.stderr)
            continue
        if res.truncated:
            failed += 1
            print(f"FAIL(truncated) {f}", file=sys.stderr)
            continue
        # Index the document in ``files`` (L0/L1 + vault file) so the Memories
        # page and the L0–L2 reader work. The chunks above already carry the
        # embeddings, so we only index here — no second embedding pass. A single
        # git commit at the end keeps the vault cheap (commit=False per file).
        try:
            fs.write(uri, content, tenant=args.tenant, metadata={"title": rel.stem}, commit=False)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL(index) {f}: {e}", file=sys.stderr)
            continue
        ok += 1
        done.add(key)
        if ok % 50 == 0:
            _save_state(state, done)
            rate = seen / max(1e-6, time.time() - t0)
            print(f"  progress: {seen} seen, {ok} ok, {failed} failed, {skipped} skipped ({rate:.1f}/s)")
        if args.sleep > 0:
            await asyncio.sleep(args.sleep)
        if args.limit and ok >= args.limit:
            print("limit reached")
            break

    _save_state(state, done)
    # One commit for the whole batch (each write() ran with commit=False).
    try:
        from stratum.filesystem import git_commit

        sha = git_commit(settings.kb_path, f"vault import: {ok} files ({args.tenant})")
        if sha:
            print(f"vault committed: {sha}")
    except Exception as e:  # noqa: BLE001
        print(f"WARN: vault commit failed: {e}", file=sys.stderr)
    print(f"DONE: total={seen} ok={ok} failed={failed} skipped={skipped} in {time.time()-t0:.0f}s")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a local markdown vault into Stratum.")
    ap.add_argument("root", help="vault directory")
    ap.add_argument("--mode", choices=["structure", "ecl"], default="structure")
    ap.add_argument("--tenant", default="default")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
