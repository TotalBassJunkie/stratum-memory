# PROGRESS — Stratum-Memory (autonomer Umsetzungslauf)

Status je Arbeitspaket. Legende: ⬜ offen · 🟦 in Arbeit · ✅ fertig (Gate bestanden) · ⛔ blockiert

| WP | Titel | Plan | Plan-Review | Build | Impl-Review | Koordinator-Beweis | Commit |
|---|---|---|---|---|---|---|---|
| WP-00 | Domain-Modell & ADRs | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-01 | Monorepo-Scaffold & Compose | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-02 | DB-Schema & Migrationen | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-03 | Storage/LLM-Abstraktion | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-04 | Filesystem-Layer + Git-Vault | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-05 | Vector/KV + Add-only-Dedup | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-05b | Job-Queue-Runner | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-06 | Temporal-Graph-Layer | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-07 | ECL-Ingestion (+Episodes) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-08 | Staging/Curator | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-09 | Reflect-Batch-Worker | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-10 | Query-Engine | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-11 | MCP-Server + Tool-API | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-12 | Multi-Tenant & ACL (RLS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-13 | Observability & Cost-Metering | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-14 | Pi-Deployment-Profil | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-14b | Server-Profil (Caddy) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-15 | Backup/Restore | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-16 | Benchmark-Harness | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| WP-17 | Härtung, E2E, Doku | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Alle 20 Arbeitspakete abgeschlossen (Gate bestanden).**

## Log

- 2026-10-11: Repo initialisiert, Scaffold (WP-01) und ADRs (WP-00) angelegt; Model-Policy gesetzt.
- Welle 1: WP-02…WP-06 (Schema/RLS, LLM-Client, Filesystem, Vector/KV, Graph) — 121 Tests, 1 Review-Runde (LLM-Cache, Graph-FK).
- Welle 2: WP-05b, WP-07, WP-08, WP-10, WP-12, WP-13 — 260 Tests, 3 Reviews (Curator-Gate-Bypass, Ingestion-Budget, Auth-Commit = BLOCKER).
- Welle 3: WP-09, WP-11, WP-15 — 424 Tests, 3 Reviews (MCP promote-scope-Bypass + Hard-Forget = BLOCKED; Reflect-Lock; Backup-Retention).
- Welle 4: WP-14, WP-14b, WP-16 — 508→538 Tests, 2 Reviews (Server-worker vector-type, Schema-Parametrisierung, Benchmark-Curator/Fehlerbehandlung).
- Welle 5: WP-17 (E2E, Doku, Coverage 84%) — 1 Review (fehlende /health+/metrics, FilesystemStore.delete).
- **Echte-DB-Verifikation** (Docker/Postgres 18): 3 produktionsrelevante Bugs gefunden & behoben, die Fake-Tests nicht sahen — RLS-Skript still zurückgerollt (uri_registry-Spalte), UUID/str-Typ-Mismatch, contradiction_check-SQL. PG18-Datenverzeichnis korrigiert.
- **Endstand: 580 Tests grün (inkl. Integration), Live-MCP-Server über HTTP verifiziert (initialize, tools/list = 10 Tools, stratum_health → ok), /health + /metrics live.**
