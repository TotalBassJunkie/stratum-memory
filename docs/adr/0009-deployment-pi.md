# ADR-0009: Deployment Pi 4 / Serverprofil, Swap & Speichertypen

**Status:** accepted

## Kontext
Zielhardware ist ein Raspberry Pi 4 mit 8 GB RAM, 24/7, microSD nur für Boot. Bekanntes Problem:
DB + zram-Swap führen zu Thrashing.

## Entscheidung
- Drei Container: `stratum-db` (2 GB), `stratum-core` (1 GB), `stratum-worker` (1.5 GB).
- **DB swapfrei**: `mem_limit: 2g` + `memswap_limit: 2g` (Service-Ebene; `deploy.resources` kennt
  kein Swap-Feld). Core/Worker dürfen swappen.
- **zram** 1 GB Prio 100 (System-Pages), **SSD-Swapfile** 4 GB Prio 10 (24/7). **Kein microSD-Swap**.
- `vm.swappiness=10`, `vm.vfs_cache_pressure=50`.
- Pi-Zielmenge: ≤ 150k Fakten / ≤ 50k Chunks; `halfvec` (fp16); Off-Pi-Reindex > 200k Vektoren.
- Serverprofil: `STRATUM_VECTOR_TYPE=vector`, mehr `shared_buffers`/`max_connections`, Caddy/TLS.

## Konsequenzen
- Stabile DB-Latenz, kein OOM/Thrashing; RAM-Budget rechnet auf 8 GB auf.
