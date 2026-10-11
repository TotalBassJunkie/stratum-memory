# Build-Kit — verbindlicher Kontext für alle Builder/Reviewer-Subagenten

Dieses Dokument MUSS von jedem Subagenten vor der Arbeit gelesen werden. Es fasst die
unveränderlichen Rahmenbedingungen zusammen. Bei Konflikt gilt `MASTER-PLAN.md` > dieses Dokument.

## Repository
Wurzel: das Repository-Stammverzeichnis (Projektwurzel)
- `src/stratum/` — Python-Paket (Python 3.12 Ziel, lokal läuft 3.15)
- `init/` — DB-Bootstrap-SQL (001_extensions, 002_app_role, 003_schema, 004_rls)
- `tests/` — pytest (`pythonpath = ["src"]`)
- `docker-compose.yml`, `Dockerfile`, `pyproject.toml`

## Unveränderliche Fakten
1. **URI-Schema**: `stratum://<bereich>/<tenant>/<pfad>`, bereich ∈ {docs,facts,entities,episodes,skills,syntheses}, Segmente `[a-z0-9._~-]+`. Implementiert in `stratum/uri.py` (NICHT neu erfinden, importieren).
2. **DB-Zugriff**: NUR über `stratum/db.py` (`connection(tenant)`, `fetch_all`, `execute`). App-Rolle ist `stratum_app` (RLS-unterworfen). Für Migrationen/Admin `connection(bypass_rls=True)`.
3. **Bi-temporale Fakten**: Tabellen `facts` (siehe init/003_schema.sql). Invarianten: höchstens ein offener Fakt (`valid_to IS NULL AND tx_to IS NULL`) je `(tenant, subject_entity_id, predicate)`; **close-before-insert** immer.
4. **memory_events** ist append-only; jede Schreiboperation erzeugt ein Event (op ∈ ADD|UPDATE|DELETE|NOOP|PROMOTE|ARCHIVE|FORGET|REFLECT).
5. **Embeddings**: `halfvec(1024)` (Pi-Default). Dimension aus `Settings.embedding_dim`.
6. **Dedup**: Cosine-Ähnlichkeit ≥ `Settings.dedup_threshold` (0.92) ⇒ ADD verboten.
7. **Curator-Regeln**: siehe MASTER-PLAN §8. Max `Settings.curator_max_promotions_per_run` (200)/Lauf.
8. **Lizenz**: Apache-2.0. Nur konzeptuelle Übernahme aus AGPL (OpenViking) und unlizenziertem Repo — KEIN Code kopieren.

## Code-Konventionen
- Python 3.12, `from __future__ import annotations`, Type Hints.
- Ruff (line-length 100), mypy-freundlich, keine externen Dienste außer Postgres + LLM-API.
- Tests mit pytest; DB-nutzende Tests via `testcontainers[postgres]` und dem `pgvector/pgvector:0.8.6-pg18`-Image.
- Neue Module in `src/stratum/<bereich>.py`; Tests in `tests/test_<bereich>.py`.

## Definition of Done (DoD) je WP
- Code + Tests vorhanden, `pytest -q` grün (soweit lokal lauffähig).
- Keine Secrets im Code; Konfiguration über `Settings`.
- Docstrings an öffentlichen Funktionen.
- Der Subagent liefert einen knappen Bericht: geänderte Dateien, Testbefehl, Testergebnis.

## Beweis-Regel
Der Builder führt Tests tatsächlich aus und zitiert die Ausgabe. Der Reviewer prüft unabhängig
(liest Code, führt Tests aus, sucht Widersprüche) und liefert ein Verdict APPROVED/REVISE/BLOCKED.
