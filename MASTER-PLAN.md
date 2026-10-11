# Master-Plan: Stratum-Memory

> Version 1.0 — final. Dieses Dokument ist die vollständige, eingefrorene Spezifikation für die Umsetzung durch Coding-Agenten. Alle Entscheidungen sind getroffen und begründet. Es gibt keine offenen Fragen, keine Platzhalter und keine nachträglichen Anpassungen.

---

## 1. Executive Summary

Stratum-Memory ist ein lokal-first betreibbares AI-Memory- und Context-System, das die Stärken von OpenViking (Filesystem-Paradigma, L0–L2), Mem0 (Add-only-Dedup, Hybrid-Retrieval), Zep/Graphiti (bi-temporaler Graph), Letta (agent-kontrollierte Tool-API) und Hindsight (Retain/Recall/Reflect-Synthese) in einer einheitlichen Architektur vereint und deren Schwächen vermeidet. Kern ist ein Unified-URI-Namespace `stratum://<bereich>/<tenant>/<pfad>` über vier Ebenen: Filesystem (transparent, L0–L2), bi-temporaler Graph (Gültigkeits- plus Transaktionszeit), Vector/KV (Ähnlichkeit + Add-only-Dedup) und Reflect/Synthesis (periodische Verdichtung). Eine ECL-Ingestion-Pipeline speist neue Daten ein, ein Staging/Curator-Gate verhindert Memory-Verschmutzung, und eine MCP-kompatible Agent-Tool-API (`promote/archive/forget/query/write`) verbindet beliebige Agenten (Claude Code, Cursor, Codex, Hermes). Der Stack ist reines Python + PostgreSQL mit pgvector — keine eigene Graph-DB, kein GPU-Zwang — und läuft identisch in Docker auf einem Raspberry Pi 4 (8 GB) wie auf einem Multi-Tenant-Server. Lizenz: Apache-2.0 (Code) bzw. MIT (Schemata), ausschließlich konzeptuelle Übernahme aus AGPL-3.0-Projekten. Ziel: minimale Infrastruktur, maximale Transparenz und echte Zeitachse.

---

## 2. Architekturübersicht

Stratum-Memory besteht aus vier Speicher-Ebenen, drei Verarbeitungsstufen und einer Agent-Schnittstelle. Alle Komponenten laufen im selben Repository (`stratum/`), sind als Python-Module getrennt und über eine gemeinsame `Storage`-Abstraktion miteinander verbunden.

### 2.1 Ebene 1 — Filesystem-Layer
Dateien (Markdown + YAML-Frontmatter) sind die **menschenlesbare Source of Truth** für Wissensdokumente, Skills und Episoden-Prosa. Jede Datei liegt unter einem Pfad, der 1:1 einer `stratum://`-URI entspricht. Der Layer erzeugt beim Schreiben automatisch drei Ladestufen:
- **L0**: einzeilige Abstraktion (≤ 280 Zeichen),
- **L1**: strukturierte Kurzfassung (≤ 200 Wörter, Schlüssel-Entitäten + Kernfakten),
- **L2**: vollständiger Inhalt.

Das Verzeichnis wird per Git versioniert (ein Bare-Repo bzw. Remote pro Deployment), sodass jede Änderung nachvollziehbar und rückrollbar ist. Metadaten (Pfad, Hash, Ladestufen, Tenant) werden zusätzlich in der Tabelle `files` gehalten, damit Abfragen ohne Dateisystem-Scan funktionieren.

### 2.2 Ebene 2 — Temporal-Graph-Layer
Fakten werden als **bi-temporale Kanten** über Entitäten modelliert. Jede Kante hat eine Gültigkeitsachse (`valid_from`/`valid_to` — wann der Fakt in der Welt wahr war) und eine Transaktionsachse (`tx_from`/`tx_to` — wann das System ihn wusste). Falsch gewordene Fakten werden nicht gelöscht, sondern `valid_to` wird geschlossen; Korrekturen erzeugen eine neue Kante und schließen die alte in der Transaktionszeit. Der Graph wird **relational in PostgreSQL** abgebildet (Tabellen `entities`, `facts`, `edges`), Traversierung über rekursive CTEs. Es wird bewusst keine eigene Graph-DB (Neo4j/FalkorDB) eingesetzt, um Latenz, Betriebs- und Speicheraufwand auf dem Pi zu minimieren.

### 2.3 Ebene 3 — Vector/KV-Layer
pgvector speichert Embeddings für Fakten, Dokument-Chunks und Entitäten. Der KV-Anteil (`kv`-Tabelle) hält kleine, schnelle Schlüssel-Wert-Zustände (Session-Cursor, Feature-Toggles je Tenant, Short-Term-Memory). Der **Add-only-Dedup-Algorithmus** (nach Mem0-Vorbild) verhindert Duplikate: bei jedem Add wird per Cosine-Ähnlichkeit (Schwelle 0.92) nach Kandidaten gesucht und ein Klassifikator (ADD / UPDATE / DELETE / NOOP) entschieden. Jede Änderung landet zuerst im append-only `memory_events`-Log.

### 2.4 Ebene 4 — Reflect/Synthesis-Layer
Ein **Batch-Worker** verdichtet periodisch Rohfakten und Episoden zu Synthesen: Clusterbildung, Mustererkennung, Zusammenfassungs- und Skill-Vorschläge. Ergebnisse gehen **nie direkt** in den Aktivbestand, sondern immer durch das Curator-Gate. Reflect ist hart kostenbegrenzt (Token-Budget pro Lauf) und wird per Zeitplan und Bedarfs-Trigger gestartet.

### 2.5 ECL-Ingestion-Pipeline (davor)
**Extract → Cognify → Load**: Extract zerlegt Quellen (Markdown, Chat-Sessions, URLs, PDF/DOCX, Git-Repos) in Chunks und Rohkandidaten; Cognify erzeugt Embeddings, löst Entitäten auf und extrahiert per LLM bi-temporale Fakten; Load schreibt alles in `staging_candidates`.

### 2.6 Staging/Curator-Validierung (dazwischen)
Ein Gate prüft jeden Kandidaten gegen Dubletten, Widersprüche, Konfidenz, Provenienz, Sicherheit und Tenant-Zugehörigkeit. Nur bestandene Kandidaten werden promoviert (Aktivbestand + Filesystem + Events).

### 2.7 Agent-Tool-API via MCP (darüber)
Ein FastMCP-Server exponiert die Operationen `write`, `query`, `get`, `promote`, `archive`, `forget`, `link`, `session_append`, `reflect`, `health`. Die API ist transport-agnostisch (stdio für Claude Code/Cursor/Codex, Streamable HTTP für Hermes/Server) und mit Multi-Tenant-Scopes gesichert.

### 2.8 Querschnitt
Ein `Tenant/ACL`-Modul (Postgres Row-Level-Security), ein `LLM/Embedding`-Client-Abstraktionsmodul (FreeLLMAPI-kompatibel, mit Retry/Cache/Cost-Metering) und ein `Observability`-Modul (Health-Endpunkte, Token-/Kosten-Zähler) begleiten alle Ebenen.

---

## 3. Reuse-Matrix

### 3.1 Geprüfte Kern-Repositories

| Komponente | Repo | Lizenz | Reife (★ / letzter Commit) | Übernahmeumfang | Anpassungen für Stratum-Memory |
|---|---|---|---|---|---|
| Filesystem-/URI- und L0–L2-Konzept | github.com/volcengine/OpenViking | AGPL-3.0 (inkompatibel) | 39.6k★ / 2026-10-10 | **Nur Konzept**: URI-Idee, L0–L2-Ladestufen, Self-Evolving-Gedanke | Clean-Room-Neuimplementierung; **kein Code, keine Dateien** übernommen |
| Vector/KV-Layer, Dedup-Algorithmus | github.com/mem0ai/mem0 | Apache-2.0 (kompatibel) | 66.9k★ / 2026-10-07 | Dedup-Logik (ADD/UPDATE/DELETE/NOOP), Memory-Typen, Hybrid-Retrieval-Idee | Backend von Qdrant auf pgvector/Postgres umgestellt; Embedding-Client auf eigene Abstraktion |
| Portable Session-Speicherung | github.com/mem0ai/openmemory | MIT (kompatibel) | 97★ / 2026-07-29 | **Nur Dateiformat/Konzept** für Session-Export | Geringe Reife: kein Code; eigenes `session`-Schema abgeleitet |
| Temporal-Graph-Layer, bi-temporales Schema | github.com/getzep/graphiti | Apache-2.0 (kompatibel) | 31.6k★ / 2026-10-10 | bi-temporales Datenmodell (valid_time + tx_time), Edge-Invalidierung, Provenienz | Neo4j→Postgres relational; Latenz-Optimierung (Batch, Indizes); kein Zep-Cloud-Bezug |
| Ingestion-Pipeline (ECL) | github.com/topoteretes/cognee | Apache-2.0 (kompatibel) | 32.0k★ / 2026-10-08 | ECL-Schrittstruktur, Chunking-Ansatz, Doc-Parser-Anbindung | Leichtere, token-budgetierte Variante; harte Kostenschranken statt freier Graph-Aufbau |
| Reflect-Layer, Stack | github.com/vectorize-io/hindsight | MIT (kompatibel) | 48.2k★ / 2026-10-10 | Retain/Recall/Reflect-Konzept, PostgreSQL+pgvector-Referenzstack | Reflect als gecappter Batch statt inline; keine Runtime-Abhängigkeit |
| Agent-Tool-API (Core/Recall/Archival) | github.com/letta-ai/letta | Apache-2.0 (kompatibel) | 25.1k★ / 2026-09-10 | **Nur Konzept** der Memory-Block-Tool-Semantik | Kein Letta-Runtime; eigene MCP-Tools; Drift-Schutz durch Curator |
| MCP-Boilerplate (Server + Tools) | github.com/modelcontextprotocol/servers | Apache-2.0 (neues Code) + MIT (altes Code); Docs CC-BY-4.0 → **Apache-2.0-kompatibel** | 91.1k★ / 2026-10-11 | Server-Gerüst, Tool-Definitions-/Annotationsstil, Referenzserver | Eigene Tools/MCP-Framework FastMCP; nur Struktur |
| Tiered-Memory-Vergleichsreferenz | github.com/jeremykamber/strata-memory | **Keine Lizenz** (All Rights Reserved) | 3★ / 2026-06-18 | **Nur Konzeptprüfung** (Lebenszyklus-Idee) | Kein Code, keine Texte; reine Ideenabsicherung |
| Vektor-Extension | github.com/pgvector/pgvector | PostgreSQL License (BSD-artig, kompatibel) | 23.3k★ / 2026-10-01 | Extension als Dependency | keine |

**Lizenzregel:** Aus OpenViking (AGPL-3.0) und jeremykamber/strata-memory (keine Lizenz) wird ausschließlich abstrakt gelernt. Kein Code-Fork, kein Copy-Paste, keine abgeleiteten Dateien. Alle übernommenen Code-/Schema-Elemente stammen aus Apache-2.0- oder MIT-Repos und bleiben damit mit der Projektlizenz (Apache-2.0) kompatibel.

### 3.2 Zusätzlich aktiv recherchierte Quellen (Infrastruktur)

| Fund | Quelle/Typ | Lizenz/Status | Übernahmeumfang | Anpassungen |
|---|---|---|---|---|
| pgvector+Postgres-Docker-Image | `pgvector/pgvector:0.8.6-pg18` (Docker Hub) | PostgreSQL/BSD-artig | Basis-Image, gepinnte Tags | Memory-Tuning via `command: -c ...` (nicht per Fake-Env-Vars) |
| Bitemporale DB-Schemata | Fachartikel/Best Practices (valid_time/tx_time Muster) | öffentlich | Spaltenmodell, Invarianten | auf Postgres relational gemappt |
| Git-versionierte Markdown-KB | etabliertes Muster (Zettelkasten/Obsidian-Vault + Git) | — | Verzeichnisstruktur, Frontmatter | in Storage-Layer automatisiert |
| MCP-Boilerplates | modelcontextprotocol/servers + FastMCP-Dokumentation | Apache-2.0/MIT-Mix (Repo), FastMCP Apache-2.0 | Tool-Gerüst, stdio/HTTP-Transport | eigene Tool-Definitionen |
| Pi-optimierte Compose-Vorlagen | Docker/Debian-Homelab-Guides, hvr.biz, gntech | öffentlich | Resource-Limit-Muster, Swap-Setup | 8-GB-Profil für Stratum geschnitten |
| pgvector-Tuning (HNSW/shm_size) | Markaicode/Produktions-Guides | öffentlich | `shm_size`, `maintenance_work_mem`-Hinweise | auf Pi-RAM skaliert |

---

## 4. Technologie-Stack-Entscheidungen mit Begründung

| Bereich | Entscheidung | Begründung |
|---|---|---|
| Sprache Core | **Python 3.12** | Bester Ökosystem-Fit (mem0/graphiti/cognee sind Python), starke LLM-/Embedding-Libs, FastMCP nativ. |
| Datenbank | **PostgreSQL 18 + pgvector 0.8** | Eine DB für KV, Vektor, Graph (relational), Temporal, Files-Metadaten. Eliminiert Zeps Schwäche „eigene Graph-DB nötig“ und reduziert RAM/Betrieb auf dem Pi drastisch. |
| Graphmodell | **Relationale bi-temporale Tabellen + rekursive CTEs** | Vermeidet Neo4j/FalkorDB (~300 MB+ JVM/Base-RAM) auf dem Pi; ausreichend performant für die Pi-Zielmenge (≤ 150 k Fakten / ≤ 200 k Kanten), auf dem Server skaliert derselbe Code auf Millionen Kanten. |
| Vektorindex | **HNSW** (pgvector), Typ `halfvec(1024)` auf dem Pi | Bestes Recall/Latenz-Verhältnis; fp16 halbiert Speicher bei vernachlässigbarem Recall-Verlust und passt 200 k Vektoren sicher in 2 GB. IVF-Flat bleibt als Rückfall bei Speicherdruck. |
| Embeddings | **Externe Embedding-API** (FreeLLMAPI-kompatibel), dimension 1024 | Regel „keine lokale GPU-Inferenz auf dem Pi“; Embeddings werden in DB gecacht, um Wiederholungskosten zu senken. |
| LLM-Zugriff | **Externe Chat-API** über eigenen `LLMClient` (Retry, Timeout, Cost-Metering, Response-Cache) | FreeLLMAPI-kompatibel; erlaubt Modellwechsel ohne Codeänderung; harte Kostenkontrolle. |
| Ingestion | **Eigene ECL-Pipeline** (cognee-inspiriert) | Volle Kontrolle über Token-Budget und Staging; cognee bleibt Referenz, nicht Runtime. |
| Queue/Jobs | **Postgres-Tabelle + `SELECT ... FOR UPDATE SKIP LOCKED`** | Kein Redis/RabbitMQ nötig → weniger RAM/Container auf dem Pi; transaktional konsistent. |
| MCP-Framework | **FastMCP (Python)** | Erste-Wahl-Boilerplate, stdio + Streamable-HTTP, minimale Abhängigkeiten. |
| Filesystem/Versionierung | **Lokales Git-Repo** (Markdown + Frontmatter) | Transparenz, Diffbarkeit, Rollback; erfüllt „git-versionierte Wissensspeicher“. |
| API-Keys/ACL | **Gehashte Keys in DB + Postgres RLS** | Row-Level-Security erzwingt Tenant-Isolation auf DB-Ebene (Defense in Depth). |
| Observability | **`/health`, `/metrics` (Prometheus-Textformat), strukturierte JSON-Logs** | Leichtgewichtig, Pi-tauglich, ohne schweren Stack. |
| Containerisierung | **Docker Compose (Compose-Spec, ohne Top-Level `version:`)** | Standard auf Pi und Server; reproduzierbare Limits. |
| Tests | **pytest + testcontainers (Postgres/pgvector)** | Integrationsnähe ohne Produktionsrisiko; einheitlich für alle WPs. |
| Lizenz | **Apache-2.0** (Code), MIT-kompatible Schemata | Kompatibel mit allen übernommenen Quellen; schützt gegen AGPL-Verseuchung. |

**Verworfene Alternativen (final):** Neo4j/FalkorDB (RAM), Qdrant/Weaviate (zusätzlicher Dienst), SQLite (kein RLS/HNSW in benötigtem Umfang), Redis (unnötig), Letta-Runtime (Context-Drift + Betriebsgewicht), lokale Embedding-Modelle auf dem Pi (Ressourcen).

---

## 5. Datenmodell

### 5.1 URI-Schema
Das kanonische Schema lautet (überall im Dokument einheitlich):
```
stratum://<bereich>/<tenant>/<pfad>
```
- `<bereich>` ∈ {`docs`, `facts`, `entities`, `episodes`, `skills`, `syntheses`} — **erstes** Segment
- `<tenant>` — **zweites** Segment (Multi-Tenant-Isolation), Default `default`
- `<pfad>` — hierarchisch, mit `/` getrennt, ohne führende/abschließende Slashes

Beispiele:
```
stratum://docs/default/projects/stratum/architecture
stratum://facts/acme/customer/42/plan-pro
stratum://entities/acme/person/ada-lovelace
stratum://episodes/default/2026/10/session-abc123
stratum://skills/default/stratum/sql-tuning
stratum://syntheses/default/2026-w41/pattern-report
```
**Regeln:** URIs sind lower-case, `[a-z0-9._~/-]+` (Zeichen `=`, Leerzeichen und Großbuchstaben sind **nicht** erlaubt), max. 255 Zeichen, keine `..`-Segmente. Jede URI ist global eindeutig und wird in `uri_registry(uri PK, bereich, tenant, kind, ref_id)` geführt. `ref_id` verweist auf den zugehörigen Datensatz (file_id/fact_id/entity_id/…).

### 5.2 Bi-temporales Faktenschema (`facts`)
| Spalte | Typ | Bedeutung |
|---|---|---|
| `fact_id` | uuid PK | Identität |
| `tenant_id` | text | Tenant (RLS) |
| `subject_entity_id` | uuid FK→entities | Subjekt |
| `predicate` | text | Relation/Attribut |
| `object_text` | text NULL | Objekt als Literal |
| `object_entity_id` | uuid NULL FK→entities | Objekt als Entität |
| `valid_from` | timestamptz | Weltzeit: Beginn |
| `valid_to` | timestamptz NULL | Weltzeit: Ende (NULL = offen) |
| `tx_from` | timestamptz NOT NULL DEFAULT now() | Systemzeit: Beginn der Kenntnis |
| `tx_to` | timestamptz NULL | Systemzeit: Ende (NULL = aktuell) |
| `confidence` | real | 0..1 |
| `source_uri` | text | Provenienz |
| `source_span` | jsonb | Zeichen-/Zeitbereich in Quelle |
| `superseded_by` | uuid NULL FK→facts | Nachfolger bei Korrektur |
| `embedding` | halfvec(1024) (Pi) / vector(1024) (Server) | Ähnlichkeitssuche; fp16 auf dem Pi |
| `created_at` | timestamptz | Audit |

**Invarianten:**
1. `valid_from ≤ valid_to` (falls `valid_to` gesetzt), `tx_from ≤ tx_to` (falls `tx_to` gesetzt) — durch CHECK-Constraints erzwungen.
2. Pro `(tenant_id, subject_entity_id, predicate)` existiert **höchstens ein offener Fakt zum Schreibzeitpunkt** (d.h. höchstens eine Zeile mit `valid_to IS NULL AND tx_to IS NULL`). Durchsetzung DB-seitig per partiellem Unique-Index:
   ```sql
   CREATE UNIQUE INDEX facts_one_open_per_pred_idx
     ON facts (tenant_id, subject_entity_id, predicate)
     WHERE valid_to IS NULL AND tx_to IS NULL;
   ```
3. **Keine zukunftsbefristeten Doppel-Offenheiten:** Ein zukünftig beginnender Fakt (`valid_from > now()`) ist ein **eigener** Fakt zu einem **anderen** `predicate`-Slot (z.B. `plan_next`) oder muss den aktuell offenen Fakt explizit per `valid_to` schließen. Der Curator verbietet einen zweiten offenen Fakt mit gleichem `predicate`.
4. **Statement-Reihenfolge (verbindlich):** Weil ein partieller Unique-**Index** in PostgreSQL nicht `DEFERRABLE` sein kann, gilt für jedes UPDATE/korrektur-Schreiben: **erst den alten Fakt schließen** (`UPDATE facts SET valid_to=…, tx_to=…, superseded_by=<neu> WHERE …`), **dann den neuen Fakt einfügen** (`INSERT …`). Bei umgekehrter Reihenfolge würde der Index-Constraint noch im selben Statement greifen und die Transaktion abbrechen. Diese „close-before-insert“-Regel ist Teil der Curator-Implementierung (§8).

**Deployment-abhängige Spaltentypen (parametrisiert):** Der Embedding-Typ ist `halfvec(1024)` im Pi-Profil und `vector(1024)` im Server-Profil. Die Alembic-Migration liest dafür die Umgebungsvariable `STRATUM_VECTOR_TYPE` (`halfvec` Default, `vector` für Server) und erzeugt den entsprechenden Spaltentyp; ein `pg_dump`-Restore zwischen den Profilen konvertiert explizit (`ALTER TABLE … ALTER COLUMN embedding TYPE vector(1024) USING embedding::vector` bzw. umgekehrt) und ist **nicht** binärkompatibel. Der Off-Pi-Reindex-Weg (§16.3) nutzt genau diese Konvertierung.

`entities`: `entity_id uuid PK`, `tenant_id`, `name`, `canonical_uri`, `type`, `aliases text[]`, `embedding halfvec(1024)` (Pi) / `vector(1024)` (Server), `created_at`, `tx_from`, `tx_to`.
`edges` (explizite Graphkanten, ergänzend zu Fakten): `edge_id`, `tenant_id`, `src_entity_id`, `dst_entity_id`, `rel_type`, `valid_from/to`, `tx_from/to`, `fact_id NULL`.

### 5.3 L0–L2-Struktur (`files` + Dateisystem)
Datei-Frontmatter (YAML):
```yaml
uri: stratum://docs/default/projects/stratum/architecture
tenant: default
bereich: docs
title: Stratum-Architektur
l0: "Vier-Ebenen-Memory-System mit bi-temporalem Graph, pgvector und MCP-API."
l1: |
  Stratum-Memory vereint Filesystem-, Temporal-Graph-, Vector/KV- und
  Reflect-Layer über eine URI. Ingestion via ECL, Curator-Gate, MCP-API.
tags: [memory, architecture]
updated_at: 2026-10-11T09:00:00Z
```
Tabelle `files`: `file_id uuid PK`, `tenant_id`, `uri`, `path`, `content_hash`, `l0 text`, `l1 text`, `size_bytes bigint`, `git_commit`, `updated_at`, `embedding halfvec(1024)` (über L1; Pi) / `vector(1024)` (Server).
**Ladepolitik:** `query` liefert standardmäßig L0; `level="l1"`/`level="l2"` fordert mehr; L2 wird nur explizit geladen (Transparenz + Tokensparen).

### 5.4 Weitere Tabellen
- `chunks`: `chunk_id`, `tenant_id`, `source_uri`, `ordinal`, `text`, `embedding halfvec(1024)` (Pi) / `vector(1024)`, `content_hash`.
- `kv`: `tenant_id`, `key`, `value jsonb`, `expires_at NULL`, PK `(tenant_id,key)`.
- `staging_candidates`: `candidate_id`, `tenant_id`, `kind` (fact/doc/entity/synthesis), `payload jsonb`, `source_uri`, `confidence`, `status` (pending/approved/rejected), `reason`, `created_at`.
- `memory_events` (append-only): `event_id`, `tenant_id`, `op` (ADD/UPDATE/DELETE/NOOP/PROMOTE/ARCHIVE/FORGET/REFLECT), `target_uri`, `before jsonb`, `after jsonb`, `actor`, `created_at`.
- `syntheses`: `synthesis_id`, `tenant_id`, `scope_uri`, `summary`, `payload jsonb`, `model`, `tokens_used`, `created_at`.
- `jobs`: `job_id`, `kind`, `payload`, `status`, `attempts`, `run_after`, `locked_by`, `locked_at`.
- `tenants`, `api_keys`, `audit_log`, `uri_registry`.

---

## 6. API-Spezifikation (MCP-Tools)

Alle Tools akzeptieren implizit `tenant` (aus dem Auth-Kontext). Rückgaben sind JSON. `level ∈ {l0,l1,l2}`. Zeitstempel ISO-8601.

```jsonc
// write — Inhalt schreiben (durchläuft Ingestion + Curator)
stratum_write(
  uri: string,                       // stratum://<bereich>/<tenant>/<pfad>
  content: string,                   // Markdown/JSON
  kind: "doc"|"fact"|"episode"|"skill",
  metadata?: object,                 // tags, valid_from, source_uri, ...
  auto_promote?: boolean = false     // true nur für vertrauenswürdige Aufrufer
) -> { uri: string, candidate_id?: string, status: "staged"|"promoted", l0: string }

// query — Hybrid-Suche mit Zeit- und Ebenen-Kontrolle
stratum_query(
  query: string,
  scope?: string = null,             // z.B. "stratum://docs/default/projects/stratum"
  mode?: "hybrid"|"vector"|"graph"|"kv" = "hybrid",
  k?: integer = 8,
  level?: "l0"|"l1"|"l2" = "l0",
  as_of_valid?: string = null,       // Weltzeit-Sicht
  as_of_tx?: string = null,          // Systemzeit-Sicht
  filters?: object = {}              // tags, bereich, confidence_min
) -> { results: Array<{ uri, level, score, content, valid_from?, valid_to?, provenance }> }

// get — Direktzugriff auf eine URI
stratum_get(uri: string, level?: "l0"|"l1"|"l2" = "l1")
  -> { uri, level, content, metadata, valid_from?, valid_to? }

// promote — Kandidat aus dem Staging freigeben
stratum_promote(candidate_id: string, override_reason?: string = null)
  -> { uri, status: "promoted", event_id: string }

// archive — Aus dem Aktivbestand entfernen, Historie behalten
stratum_archive(uri: string, reason: string)
  -> { uri, status: "archived", event_id: string }

// forget — Recht auf Vergessen (Löschung inkl. Vektoren/Graph)
stratum_forget(uri: string, hard?: boolean = false, reason: string)
  -> { uri, mode: "soft"|"hard", deleted_refs: integer, event_id: string }

// link — Explizite temporal-graph Kante setzen
stratum_link(
  src_uri: string, dst_uri: string, rel_type: string,
  valid_from?: string = null, valid_to?: string = null, confidence?: number = 1.0
) -> { edge_id: string, fact_id: string }

// session_append — Episodisches Kurzzeitgedächtnis fortschreiben
stratum_session_append(session_id: string, role: string, content: string, metadata?: object)
  -> { uri: string, turn: integer }

// reflect — Synthese-Lauf anstoßen (kostenbegrenzt)
stratum_reflect(scope?: string = null, budget_tokens?: integer = 20000, async?: boolean = true)
  -> { job_id: string, scope: string, estimated_tokens: integer }

// health — Status
stratum_health() -> { status: "ok"|"degraded", db: boolean, git: boolean, llm: boolean, queued_jobs: integer }
```

**Fehlerformat:** `{ "error": { "code": "NOT_FOUND"|"FORBIDDEN"|"VALIDATION"|"CONFLICT"|"BUDGET_EXCEEDED"|"INTERNAL", "message": string } }`.
**Auth:** Jeder Aufruf trägt einen API-Key; Scopes `read`, `write`, `promote`, `admin`. Fehlender Scope → `FORBIDDEN`.

---

## 7. Ingestion-Pipeline-Design (ECL)

**Quellentypen (final unterstützt):** Markdown/Text-Dateien, Chat-/Agent-Sessions (JSON), URLs (Web-Fetch über `LLMClient.fetch`/`augment`-Adapter), PDF/DOCX (Text-Parser-Adapter), Git-Repos (Klon + Markdown-Extraktion), direkte MCP-`write`-Aufrufe.

**Schritt E — Extract**
1. Quelle laden, MIME bestimmen, in Text normalisieren.
2. In Chunks (Ziel 800 Tokens, 100 Overlap) zerlegen; `content_hash` je Chunk.
3. Rohkandidaten ablegen (`chunks` + vorläufige `staging_candidates`).
Dedup-Ebene 1: Chunk mit bereits vorhandenem `content_hash` wird übersprungen (kein LLM-Call).

**Schritt C — Cognify** (LLM-gebunden, budgetiert)
1. Embedding je Chunk/Entität/Fakt (`halfvec(1024)` auf dem Pi, `vector(1024)` auf dem Server — siehe §5.2).
2. Entitätsauflösung: Kandidaten-Entities per Vektorsuche + Alias-Abgleich gegen `entities`; bestehende bevorzugen.
3. Bi-temporale Faktenextraktion per LLM-Prompt (Schema-erzwungen, JSON) inkl. `valid_from`/`valid_to` und `confidence`.
4. Kostenkontrolle: harte Obergrenze Tokens/Quelle; bei Überschreitung Rest als `pending` markieren.

**Schritt L — Load**
1. Schreibe Fakten/Entities/Docs als `staging_candidates(status=pending)`.
2. Übergib an den Curator (Abschnitt 8).
3. Nur bei `approved`: Persistenz in Zieltabellen, L0–L2-Erzeugung, Git-Commit (bei Docs), `memory_events`-Eintrag.

**Idempotenz:** Jede Verarbeitung trägt einen `ingest_id`; Level-2-Dedup verhindert Doppel-Schreiben bei Wiederholung.

---

## 8. Staging/Curator-Validierungslogik

Der Curator ist die einzige Brücke vom Staging in den Aktivbestand. Er läuft synchron für `write` mit `auto_promote=false` und als Worker für Batch-Ingestion. **Promotion-Kriterien (alle müssen erfüllt sein):**

| # | Kriterium | Regel | Ablehnung → `reason` |
|---|---|---|---|
| 1 | Dedup | Ist die Cosine-Ähnlichkeit zum nächsten aktiven Fakt **≥ 0.92**, ist ADD **verboten** — der Klassifikator muss UPDATE/DELETE/NOOP wählen. ADD ist nur bei Ähnlichkeit **< 0.92** zulässig | `duplicate` (≥ 0.92 und ADD gewählt) / `noop` |
| 2 | Widerspruchsfreiheit | Kein aktiver Fakt mit gleichem `(subject, predicate)` und inkompatiblem Objekt ohne UPDATE-Absicht | `contradiction` |
| 3 | Konfidenz | `confidence ≥ 0.6` | `low_confidence` |
| 4 | Provenienz | `source_uri` gesetzt und auflösbar | `no_provenance` |
| 5 | Sicherheit | Kein PII/Secret-Muster, kein Prompt-Injection-Marker im Inhalt | `unsafe` |
| 6 | Tenant | `tenant_id` des Kandidaten == Auth-Tenant (oder Admin) | `tenant_mismatch` |
| 7 | Zeitkonsistenz | `valid_from ≤ valid_to`, `valid_from` nicht in ferner Zukunft (> 1 Jahr) | `invalid_time` |

**Ablauf:** `pending → (Prüfungen) → approved → Persistenz` oder `pending → rejected(reason)`. Bei UPDATE gilt die verbindliche Reihenfolge **close-before-insert** (siehe §5.2, Invariante 4): zuerst den alten Fakt per `UPDATE` schließen (`valid_to`/`tx_to` setzen, `superseded_by` merken), dann den neuen Fakt `INSERT`en — alles in **einer** Transaktion. Ein `INSERT` vor dem `UPDATE` ist unzulässig, weil der partielle Unique-Index in derselben Transaktion sofort prüft und die Transaktion abbricht. Jede Entscheidung erzeugt einen `memory_events`-Eintrag (ADD/UPDATE/DELETE/NOOP).

**Kontaminationsschutz:** (a) Nur der Curator schreibt in Aktivtabellen; (b) Reflect-Ergebnisse sind immer Kandidaten, nie direkt aktiv; (c) Batches werden auf **max. 200 Promotionen pro Lauf** begrenzt (Konfigurationsschlüssel `CURATOR_MAX_PROMOTIONS_PER_RUN`, Default `200`), um Lawinen zu verhindern; (d) wöchentlicher Integritätsbericht (Widerspruchs- und Dublettenquote) über `stratum_reflect`.

---

## 9. Reflect-Batchjob-Design

**Zweck:** Periodische Verdichtung von Fakten/Episoden zu Synthesen und Skill-Vorschlägen, ohne den Aktivbestand direkt zu verändern.

**Trigger (final):**
1. Zeitplan: täglich 03:00 lokal (Cron im Worker).
2. Bedarfs-Trigger: wenn `count(memory_events) seit letztem Reflect ≥ 500` ist.
3. Manuell: `stratum_reflect`.

**Kostenschranken:** Standard-Budget **20 000 Tokens pro Lauf**, hartes Maximum 50 000; Abbruch bei Überschreitung mit `BUDGET_EXCEEDED`-Event. Max. 1 Reflect-Lauf gleichzeitig (DB-Lock). Bevorzugt günstiges Modell (konfigurierbar), Chunk-Stichprobe statt Gesamtkorpus.

**Ablauf:**
1. Scope bestimmen (Tenant oder Teilbaum).
2. Letzte `memory_events` + neue Fakten laden.
3. Embedding-Cluster (HDBSCAN/k-means) → Themen.
4. Pro Cluster LLM-Zusammenfassung + Muster-/Skill-Vorschlag (JSON-Schema).
5. Ergebnisse als `staging_candidates(kind=synthesis|skill)` schreiben.
6. Curator promoviert; Synthese-Dateien landen in `stratum://syntheses/...` (Git).
7. `submitted/approved/rejected` und Tokens nach `syntheses` + `jobs` protokollieren.

**Zeitplan-Zusammenfassung:** `0 3 * * *` täglich; zusätzlich Trigger ≥ 500 Events; Timeout 30 min/Lauf.

---

## 10. Deployment-Architektur für Raspberry Pi 4 (8 GB)

### 10.1 Container-Topologie
Drei Container: `stratum-db` (Postgres 18 + pgvector), `stratum-core` (API + MCP-Server), `stratum-worker` (Ingestion + Curator + Reflect). Optional `stratum-caddy` (Reverse-Proxy, nur Server-Modus).

### 10.2 `docker-compose.yml` (Auszug, final)
```yaml
services:
  stratum-db:
    image: pgvector/pgvector:0.8.6-pg18
    restart: unless-stopped
    command: >
      postgres
      -c shared_buffers=1GB
      -c effective_cache_size=3GB
      -c work_mem=16MB
      -c maintenance_work_mem=128MB
      -c max_connections=30
      -c random_page_cost=1.1
      -c autovacuum_max_workers=1
    environment:
      POSTGRES_DB: stratum
      POSTGRES_USER: stratum
      POSTGRES_PASSWORD_FILE: /run/secrets/pg_password
    secrets: [pg_password]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./init:/docker-entrypoint-initdb.d:ro
    shm_size: "512mb"
    # memory-swap == memory => DB erhält KEINEN Swap (weder zram noch SSD).
    mem_limit: 2g
    memswap_limit: 2g
    deploy:
      resources:
        limits: { memory: 2g, cpus: "2.0" }
        reservations: { memory: 1g }
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U stratum -d stratum"]
      interval: 10s
      timeout: 5s
      retries: 10

  stratum-core:
    build: .
    restart: unless-stopped
    depends_on: { stratum-db: { condition: service_healthy } }
    environment:
      # Nicht-privilegierte App-Rolle 'stratum_app' (NICHT Tabellen-Owner, kein Superuser)
      # => RLS greift auch ohne FORCE. Passwort wird beim Start aus dem Secret injiziert.
      STRATUM_DB_USER: stratum_app
      STRATUM_DB_PASSWORD_FILE: /run/secrets/app_password
      STRATUM_DB_HOST: stratum-db
      STRATUM_DB_NAME: stratum
      # Pi-Profil: streamable-http. Im Container MUSS auf 0.0.0.0 gebunden werden,
      # sonst ist der gepublishte Port nicht erreichbar; die Beschränkung auf
      # localhost erfolgt host-seitig über das ports-Mapping (siehe §10.6).
      STRATUM_MCP_TRANSPORT: streamable-http
      STRATUM_MCP_HOST: 0.0.0.0
      STRATUM_MCP_PORT: "8088"
      LLM_BASE_URL: ${LLM_BASE_URL}
      LLM_API_KEY_FILE: /run/secrets/llm_key
    ports:
      - "127.0.0.1:8088:8088"
    secrets: [llm_key, app_password]
    volumes:
      - kb:/var/stratum/kb                    # Git-Vault (auf SSD binden)
    deploy:
      resources:
        limits: { memory: 1g, cpus: "1.5" }

  stratum-worker:
    build: .
    restart: unless-stopped
    command: ["python", "-m", "stratum.worker"]
    depends_on: { stratum-db: { condition: service_healthy } }
    environment:
      STRATUM_DB_USER: stratum_app
      STRATUM_DB_PASSWORD_FILE: /run/secrets/app_password
      STRATUM_DB_HOST: stratum-db
      STRATUM_DB_NAME: stratum
      REFLECT_BUDGET_TOKENS: "20000"
    secrets: [app_password]
    deploy:
      resources:
        limits: { memory: 1.5g, cpus: "1.5" }

volumes:
  pgdata:
  kb:
secrets:
  pg_password: { file: ./secrets/pg_password }   # für Rolle 'stratum' (Owner/Superuser)
  app_password: { file: ./secrets/app_password } # für Rolle 'stratum_app' (RLS-unterworfen)
  llm_key: { file: ./secrets/llm_key }
```

**DB-Rollen (verbindlich, WP-02):** Zwei Rollen. (1) `stratum` — Owner/Superuser (so legt das offizielle Postgres-Image `POSTGRES_USER` an), nur für Migrationen, `init/`-Skripte und `pg_dump`. (2) `stratum_app` — **nicht-privilegierte** Login-Rolle ohne `BYPASSRLS`, ohne Tabellen-Eigentum; nur diese Rolle nutzen Core und Worker. Da Tabellen-Owner RLS umgehen, werden alle Aktivtabellen von `stratum` erstellt und `stratum_app` erhält lediglich `GRANT SELECT, INSERT, UPDATE, DELETE`. **RLS greift primär, weil `stratum_app` kein Owner ist.** Zusätzlich setzt die Migration `ALTER TABLE … FORCE ROW LEVEL SECURITY` als Härtung gegen einen *künftigen* Nicht-Superuser-Owner — **Hinweis: gegen Superuser wirkt FORCE RLS nicht**, die Superuser-Rolle `stratum` ist daher niemals im normalen App-Betrieb zu verwenden. Beim Verbindungsaufbau führt Core/Worker `SET stratum.tenant = '<tenant>'` (bzw. `set_config('stratum.tenant', <tenant>, true)` für die Transaktion) aus.

### 10.6 MCP-Transport (Auflösung des Betriebsmodus)
Ein **residenter** Container kann keinen stdio-Transport anbieten — stdio setzt voraus, dass der MCP-Client den Serverprozess selbst startet und über stdin/stdout anbindet. Daher gilt final:
- **Pi-Profil (resident):** `stratum-core` spricht **Streamable-HTTP**. Der **Serverprozess im Container bindet an `0.0.0.0:8088`**; die Beschränkung auf localhost erfolgt **host-seitig** über das `ports:`-Mapping `127.0.0.1:8088:8088` (eine In-Container-Bindung an `127.0.0.1` wäre über das Port-Mapping nicht erreichbar und ist deshalb verboten). MCP-Clients (Claude Code/Cursor) verbinden sich über die HTTP-URL auf dem Host.
- **Lokaler stdio-Modus (Entwickler-PC):** Für Clients, die ausschließlich stdio können, wird der Server **nicht** als Resident-Container, sondern als client-gestarteter Prozess betrieben — entweder nativ (`python -m stratum.mcp`) oder per dokumentiertem Wrapper, der **im Compose-Netz** läuft:
  ```
  docker compose run --rm -i stratum-core python -m stratum.mcp --transport stdio
  ```
  `docker compose run` hält den Container im Projektnetz (Namensauflösung von `stratum-db` funktioniert), reicht stdin/stdout durch und erbt Env/Secrets. **Nicht** `docker run --network host` verwenden: dort löst `stratum-db` nicht auf und der DB-Port ist auf dem Host nicht veröffentlicht.
- **Server-Profil:** Streamable-HTTP hinter Caddy/TLS (siehe §10.5).

### 10.3 RAM-Budget (8-GB-Pi)
| Anteil | Verbrauch |
|---|---|
| OS + Kernel + Docker | ~1.2 GB |
| `stratum-db` | ≤ 2.0 GB (Limit) |
| `stratum-core` | ≤ 1.0 GB (Limit) |
| `stratum-worker` | ≤ 1.5 GB (Limit) |
| Puffer/Filesystem-Cache | ~2.3 GB |

**CPU:** Die Limits summieren auf 5.0 vCPU an einem 4-Kern-Pi 4; Oversubscription ist bei Docker zulässig und beabsichtigt (die Container sind nicht dauerhaft CPU-gesättigt). Falls doch CPU-Throttling auftritt, ist zuerst `stratum-worker.cpus` auf `1.0` zu senken (Reflect ist batch- und toleranzfreundlich), danach `stratum-core` auf `1.0`.

### 10.4 Swap-Konfiguration (verbindlich)
- **zram**: 1 GB, Priorität **100** — fängt kurze Systemspitzen ab. **Ausgeschlossen für die DB**: `stratum-db` läuft mit `--memory-swap == --memory` (kein Swap), damit Postgres niemals durch zram komprimiert wird (vermeidet den bekannten DB+zram-Thrashing-Effekt).
- **SSD-Swapfile**: 4 GB auf externer USB-3.0-SSD (`/mnt/ssd/swapfile`), Priorität **10** — 24/7-Überlauf. **Kein microSD-Swap** (Verschleiß + Latenz).
- **sysctl**: `vm.swappiness=10`, `vm.vfs_cache_pressure=50`, `vm.overcommit_memory=0`.
- **SSD für Daten**: `pgdata` und `kb`-Vault auf SSD binden (`/mnt/ssd/stratum/...`), nicht auf microSD.
- **Boot**: zram-Swap + SSD-Swapfile über `/etc/fstab`/systemd beim Start aktivieren.

### 10.5 Server-Variante (Multi-Tenant)
Gleiches Compose, aber: `stratum-core` mit `STRATUM_MCP_TRANSPORT=streamable-http`, **`STRATUM_VECTOR_TYPE=vector`** (Server-Profil ohne fp16, siehe §5.2), Caddy-TLS davor, `shared_buffers`↑, `max_connections`↑, PgBouncer optional, externe Postgres-Backups.

---

## 11. Sicherheits-/Zugriffskontrollkonzept

- **Authentifizierung:** API-Keys (Format `stk_<tenant>_<random>`), in `api_keys` nur als **Argon2id-Hash** gespeichert; Lookup über Schlüssel-Präfix, dann Verifikation.
- **Autorisierung/Scopes:** `read`, `write`, `promote`, `admin`. Tools prüfen Scope + Tenant; `admin` darf tenant-übergreifend.
- **Row-Level-Security:** Alle mandantenfähigen Tabellen tragen `tenant_id`; Postgres-RLS-Policies binden jede Zeile an `current_setting('stratum.tenant')`, das pro Verbindung aus dem Auth-Kontext gesetzt wird (`set_config('stratum.tenant', <tenant>, true)`). Die Hauptkontrolle ist, dass der Zugriff über die **nicht-privilegierte App-Rolle `stratum_app`** erfolgt (kein Superuser, kein `BYPASSRLS`, kein Tabellen-Owner — siehe §10.2); nur weil sie kein Owner ist, greift RLS. `ALTER TABLE … FORCE ROW LEVEL SECURITY` ist zusätzliche Härtung gegen einen *künftigen* Nicht-Superuser-Owner — **gegen Superuser wirkt FORCE RLS nicht**, daher ist die Superuser-Rolle `stratum` nie im App-Betrieb zu verwenden. Defense in Depth (auch bei Codefehlern kein Cross-Tenant-Leak).
- **Transport:** Residente Container nutzen **Streamable-HTTP**; der Prozess bindet container-intern an `0.0.0.0:8088`, die Beschränkung auf localhost erfolgt **host-seitig** über das Port-Mapping `127.0.0.1:8088:8088`. Reiner stdio-Modus läuft als client-gestarteter Prozess bzw. `docker compose run --rm -i stratum-core …` (siehe §10.6). Öffentliches HTTP nur im Server-Modus hinter TLS.
- **Secrets:** Docker-Secrets (`pg_password`, `llm_key`); keine Secrets in Env-Dateien oder Logs.
- **Forget/Löschung:** `stratum_forget(hard=true)` löscht Zeilen, Vektoren, Graphkanten und (bei Docs) die Datei inkl. Git-Commit „forget: <uri>“; Audit-Eintrag bleibt (ohne PII).
- **Audit:** `audit_log` protokolliert jede schreibende Operation mit Akteur, Ziel, Zeit.
- **PII/Secret-Filter:** Curator-Kriterium 5 blockt offensichtliche Secrets/PII; optionaler Redaktionshaken vor Persistenz.
- **Netz:** DB-Port nur an `127.0.0.1`/internes Docker-Netz; kein öffentlicher DB-Zugang.

---

## 12. Arbeitspaket-Liste (Umsetzungsreihenfolge)

**Rollen je WP (verbindlich):** `PLANNING_MODEL=deepseek-r1:14b` erstellt den Plan → `PLAN_REVIEW_MODEL=qwen3:14b` prüft den Plan unabhängig → `IMPLEMENTATION_MODEL=qwen2.5-coder:14b` baut → `IMPLEMENTATION_REVIEW_MODEL=qwen3:14b` prüft die Implementierung unabhängig. Ohne bestandene Plan- und Implementierungsprüfung gilt ein WP als nicht abgeschlossen. Der Koordinator misst Beweise selbst nach (Testlauf, nicht nur Zitat).

| WP | Titel | Abhängigkeiten | Modellrolle(n) | Akzeptanzkriterien | Aufwand | Testverfahren |
|---|---|---|---|---|---|---|
| WP-00 | Domain-Modell & Architektur-ADRs | — | Planer+Reviewer (kein Code) | `docs/adr/` mit 8 ADRs (Stack, Graph-relational, Bitemporal, Curator, Reflect-Budget, MCP, Lizenz, Deployment); `CONTEXT.md` mit Ubiquitous Language | 1 PT | Review-Checkliste; Reviewer-Verdict APPROVED |
| WP-01 | Monorepo-Scaffold & Compose-Basis | WP-00 | qwen2.5-coder / qwen3 | Repo-Struktur `stratum/`, `pyproject.toml`, `docker-compose.yml` mit `stratum-db` startet healthy, `pytest` läuft grün (leer) | 2 PT | `docker compose up -d` + `pg_isready`; `pytest -q` |
| WP-02 | DB-Schema & Migrationen (+ Rollen/RLS-Grundlage) | WP-01 | qwen2.5-coder / qwen3 | Alle Tabellen aus §5 per Alembic-Migration; `vector`/`halfvec`-Extension; Indizes (HNSW, GIN, partieller Unique-Index close-before-insert); Rollen `stratum` (Owner/Superuser) und `stratum_app` (non-owner, kein BYPASSRLS) mit Grants + `FORCE ROW LEVEL SECURITY` | 3 PT | Migration up/down; Schema-Snapshot-Test; Rollen-/Grant-Test; RLS-Negativtest |
| WP-03 | Storage/LLM-Abstraktion (Client) | WP-01 | qwen2.5-coder / qwen3 | `LLMClient` (chat/embed/fetch, Retry, Timeout, Cache, Cost-Metering) mit Fake-Backend testbar | 3 PT | Unit-Tests mit Mock-HTTP; Cache-Treffer-Test |
| WP-04 | Filesystem-Layer + Git-Vault | WP-02, WP-03 | qwen2.5-coder / qwen3 | URI↔Pfad 1:1; L0/L1/L2-Generierung; Git-Commit je Schreiben; `files`-Tabelle konsistent | 3 PT | Roundtrip-Test; Git-Log-Test; L0-Längenprüfung |
| WP-05 | Vector/KV-Layer + Add-only-Dedup | WP-02, WP-03 | qwen2.5-coder / qwen3 | Add/Update/Delete/NOOP korrekt; Cosine-Schwelle 0.92; `memory_events` append-only | 4 PT | Property-Tests; Dedup-Szenariosuite |
| WP-06 | Temporal-Graph-Layer | WP-02, WP-05 | qwen2.5-coder / qwen3 | Bi-temporale Writes/Reads; `as_of_valid`/`as_of_tx`-Abfragen; rekursive Traversierung; Unique-Invariante erzwingt | 5 PT | Zeitreise-Tests (Vergangenheit/korrigiert); Traversierungs-Benchmark |
| WP-05b | Job-Queue-Runner (SKIP LOCKED) | WP-02 | qwen2.5-coder / qwen3 | Workerschleife auf `jobs`-Tabelle mit `SELECT … FOR UPDATE SKIP LOCKED`, Retry/Backoff, Attempts-Cap, saubere Lock-Freigabe bei Absturz | 2 PT | Parallel-Worker-Test (kein Doppel-Claim); Crash-/Recovery-Test |
| WP-07 | ECL-Ingestion-Pipeline (+ Episodes/Session-Store) | WP-03, WP-04, WP-05, WP-05b, WP-06 | qwen2.5-coder / qwen3 | Alle Quellentypen aus §7 inkl. Sessions/Episodes (`stratum://episodes/...`) verarbeitet; Chunk-Dedup; Token-Budget bricht kontrolliert ab | 5 PT | Fixture-Suite je Quellentyp (inkl. Session-JSON); Budget-Abbruch-Test; Episode-Roundtrip |
| WP-08 | Staging/Curator-Validierung | WP-06, WP-07 | qwen2.5-coder / qwen3 | 7 Kriterien aus §8 durchgesetzt; close-before-insert-Reihenfolge erzwungen; UPDATE in einer Transaktion; `CURATOR_MAX_PROMOTIONS_PER_RUN=200`; jede Entscheidung als Event | 4 PT | Kriterien-Matrix-Tests; Widerspruchs-/Dubletten-Tests; Reihenfolge-/Index-Verletzungstest |
| WP-09 | Reflect-Batch-Worker | WP-08 | qwen2.5-coder / qwen3 | Trigger/Schedule/Trigger≥500; Budget 20k/50k hart; Ergebnisse nur als Kandidaten | 4 PT | Zeit-/Budget-Tests; Doppel-Lauf-Lock-Test |
| WP-10 | Query-Engine (Hybrid + L0–L2) | WP-04, WP-05, WP-06 | qwen2.5-coder / qwen3 | `mode` hybrid/vector/graph/kv; Zeitparameter; `level`-Steuerung; Scoring stabil | 4 PT | Retrieval-Qualitätstests; Scoring-Reproduzierbarkeit |
| WP-11 | MCP-Server + Agent-Tool-API | WP-04, WP-06, WP-08, WP-10 | qwen2.5-coder / qwen3 | Alle Tools aus §6 implementiert; stdio + Streamable-HTTP; Schema-validiert | 4 PT | MCP-Client-Integrationstest; Tool-Schema-Validierung; Fehlercode-Tests |
| WP-12 | Multi-Tenant & ACL (RLS) + `audit_log` | WP-02, WP-11 | qwen2.5-coder / qwen3 | API-Keys (Argon2id), Scopes, RLS-Policies mit `stratum_app` und `set_config`; `audit_log` bei jeder Schreiboperation; Cross-Tenant-Versuch schlägt fehl | 4 PT | RLS-Negativtests (über App-Rolle); Scope-Tests; Key-Hash-Test; Audit-Log-Test |
| WP-13 | Observability & Cost-Metering | WP-03, WP-11 | qwen2.5-coder / qwen3 | `/health`, `/metrics`, strukturierte JSON-Logs, Token-/Kosten-Zähler je Tenant | 2 PT | Health-Endpoint-Test; Metrik-Zähler-Test |
| WP-14 | Pi-Deployment-Profil | WP-01, WP-11 | qwen2.5-coder / qwen3 | Compose-Limits + `memswap_limit` (DB swapfrei) + Swap/zram/SSD-Setup + MCP-HTTP-Binding aus §10; Container laufen stabil; `docker stats` im Budget | 3 PT | 24h-Soak-Test auf Pi; `vmstat` si/so ≈ 0; OOM-Freiheit; `docker inspect` bestätigt `memswap_limit == mem_limit` für `stratum-db` |
| WP-14b | Server-Profil (`stratum-caddy`) | WP-14 | qwen2.5-coder / qwen3 | Compose-Overlay mit Caddy/TLS-Reverse-Proxy, Streamable-HTTP öffentlich, `shared_buffers`/`max_connections` erhöht | 1 PT | TLS-Terminierungstest; Endpoint-Erreichbarkeit |
| WP-15 | Backup/Restore & Migrationen | WP-02 | qwen2.5-coder / qwen3 | `pg_dump`+Vault-Backup; Restore-Skript; dokumentierter RTO/RPO | 2 PT | Restore-Drill in Wegwerf-Umgebung |
| WP-16 | Benchmark-Harness (LoCoMo/LongMemEval) | WP-10, WP-11 | qwen2.5-coder / qwen3 | Reproduzierbare Läufe; Metriken aus §14; Baseline-Artefakt | 4 PT | Benchmark-Report mit Zahlen; Wiedervergleich |
| WP-17 | Härtung, E2E & Dokumentation | WP-12, WP-13, WP-14, WP-15, WP-16 | qwen2.5-coder / qwen3 | E2E-Szenario (write→curate→query→reflect→forget); README/Operations-Handbuch; ≥ 80 % Coverage auf Kernmodulen | 3 PT | E2E-Test grün; Coverage-Report; Docs-Review |

**Aufwandssumme der Tabelle (20 Zeilen WP-00 … WP-17 + WP-05b + WP-14b):** **63 PT** (nachgerechnet, siehe WP-Spalte „Aufwand": 1+2+3+3+3+4+2+5+5+4+4+4+4+4+2+3+1+2+4+3). Diese PT sind **Entwicklertage exklusive Review-Wartezeit**; da pro WP vier Rollen-Gates (Planer → Plan-Reviewer → Builder → Implementierungs-Reviewer) verbindlich sind, ist die reale Kalenderzeit deutlich höher (Erfahrungswert: ×1,5–2 für sequentielle Gates). Der 24h-Soak-Test (WP-14) läuft parallel und zählt nicht als Entwicklerzeit.

**Kritischer Pfad (längster Abhängigkeitspfad):** WP-00 → WP-01 → WP-02 → WP-05 → WP-06 → WP-07 → WP-08 → WP-11 → WP-12 → WP-17 = **35 PT Längstkette**. WP-09 und WP-10 hängen **parallel** ab WP-08 bzw. WP-06 und verlängern die Kette nicht (ihre 4+4 PT zählen nicht zur Längstkette). WP-05 (und damit WP-05b) ist Pflichtglied — es wurde in der ersten Fassung zu Unrecht ausgelassen. **Server-Variante (`stratum-caddy`, §10.5)** ist ein eigenes, vom Pi-Pfad unabhängiges Deployment-Artefakt und wird über das zusätzliche WP-14b (1 PT: Caddy/TLS-Compose-Overlay + Reverse-Proxy) abgedeckt.

---

## 13. Risikoanalyse mit Gegenmaßnahmen

| Risiko | W'keit | Auswirkung | Gegenmaßnahme |
|---|---|---|---|
| Lizenzrisiko (AGPL-Verseuchung aus OpenViking) | Mittel | Hoch | Ausschließlich Clean-Room-Neuimplementierung; kein Code/Datei-Import; Lizenzprüfung je PR; ADR-007 dokumentiert Grenze |
| Lizenzrisiko (unlizenzierte Quelle jeremykamber/strata-memory) | Niedrig | Mittel | Nur konzeptionelle Lektüre, kein Code, keine Textübernahme |
| Pi-Performance (RAM/CPU/Swap) | Hoch | Hoch | Resource-Limits (§10), DB ohne Swap, SSD statt microSD, HNSW statt Vollscan, Batch statt inline, `shared_buffers` konservativ |
| Memory-Verschmutzung (falsche/doppelte Fakten) | Mittel | Hoch | Curator mit 7 Kriterien, Add-only-Dedup, Reflect nur als Kandidat, wöchentlicher Integritätsbericht |
| Context Drift (Agent-Drift durch Letta-artige Kernspeicher) | Mittel | Mittel | Kein Agent-Runtime-Eingriff; Memory nur über MCP-Tools; L0–L2 hält Kontext klein; Curator erzwingt Konsistenz |
| Reflect-Kosten (Tokenexplosion) | Mittel | Mittel | Harte Budgets (20k/50k), Stichprobe, günstiges Modell, Max. 1 Lauf, `BUDGET_EXCEEDED`-Abbruch |
| LLM-API-Ausfall | Mittel | Mittel | Retry/Backoff, Response-Cache, Degradationsmodus (nur Vektor/KV lesen, Schreiben als `pending`) |
| Datenverlust | Niedrig | Hoch | `pg_dump` + Git-Vault-Backup, SSD-Redundanz, Restore-Drill (WP-15) |
| Graph-Bloat (zu viele Kanten auf Pi) | Mittel | Mittel | Archivierung/Superseding statt Löschen, Retention-Policy, periodisches Reflect-Verdichten, Index-Wartung |
| RLS-Umgehung durch Codefehler | Niedrig | Hoch | RLS + App-seitige Tenant-Prüfung (Defense in Depth), Negativtests |
| Zeit-/Zeitzonenfehler (Bitemporal) | Mittel | Mittel | Alles `timestamptz` (UTC), Invarianten-Constraints, Zeitreise-Tests |

---

## 14. Testkonzept mit Benchmark-Plan

**Testebenen:**
1. **Unit** (pytest): Dedup-Klassifikation, Curator-Kriterien, URI-Parsing, Zeitinvarianz.
2. **Integration** (testcontainers: Postgres+pgvector): Migrationen, bi-temporale Writes/Reads, RLS, Queue-Jobs.
3. **Contract** (MCP): Tool-Schemata, Fehlercodes, Auth/Scopes.
4. **E2E:** write → curate → query → reflect → forget inkl. Git-Commit und Events.
5. **Last/Soak** (Pi): 24h-Lauf, `vmstat` (si/so ≈ 0), Speicherbudget.

**Metriken & Ziele:** Kernmodul-Coverage ≥ 80 %; p95-`query` (lokal) < 400 ms bei ≤ 100 k Fakten; Reflect-Lauf ≤ 20 k Tokens Standard.

**Benchmarks (LoCoMo / LongMemEval):**
- **LoCoMo**: Konversations-Langzeit-QA → Metriken: Antwort-Genauigkeit (F1), temporale Korrektheit.
- **LongMemEval**: Langzeit-Memory-Aufgaben (Wissensupdate, Temporal Reasoning, Multi-Session) → Metriken: Accuracy je Kategorie, „knowledge update“-Trefferquote.
- **Eigene Temporal-Suite:** Versionierte Fakten (Fakt wird korrigiert) → muss `as_of_valid`/`as_of_tx` korrekt beantworten; Contradiction-Rate = 0 nach Curator.
- **Durchführung (WP-16):** fester Seed, gepinnte Datensätze, feste Modellversion, Artefakt `benchmarks/report-<datum>.json`; Wiederholungsläufe (n=3), Median berichten.
- **Abnahmeschwelle:** Stratum schlägt eine reine Vektor-Baseline auf den Temporal-Kategorien; kein Regressionsanstieg > 2 % zwischen Releases.

---

## 15. Rollback- und Betriebsplan

**Betriebsmodell:** GitOps für Code (Tag = Release), DB-Migrationen via Alembic, Vault per Git, Container via Compose.

**Fehlschlag-Szenarien & Reaktion:**
| Szenario | Sofortmaßnahme | Rollback |
|---|---|---|
| Fehlerhafte Migration | Worker/Core stoppen | `alembic downgrade -1` aus letztem DB-Backup |
| Vergifteter Kandidat promoviert | `stratum_archive(uri)` + `memory_events` prüfen | Fakt rekonstruieren aus `memory_events.before` |
| Reflect läuft Amok (Kosten) | Worker stoppen, Job-Lock freigeben | Budget erhöhen erst nach Root-Cause |
| LLM-API-Ausfall | Degradationsmodus (nur Lesen) | Betrieb nach API-Rückkehr, `pending`-Queue abarbeiten |
| OOM-Kill auf Pi | Container-Restart-Policy greift | Limits/Faktenmenge prüfen, `shared_buffers` senken |
| Datenkorruption | DB read-only | Restore aus letztem `pg_dump` (RPO ≤ 24 h, RTO ≤ 1 h) |
| Release-Regression | `stratum-core` auf Vor-Tag | `docker compose` Image-Pin zurückrollen |

**Datenintegrität:** Transaktionen je Curator-Entscheidung; append-only `memory_events` als Wahrheitsjournal; Unique-Invariante für offene Fakten; täglicher Konsistenzjob (verwaiste Edges, offene Duplikate, fehlende Provenienz) → Report via Reflect.

**Backup:** täglich `pg_dump` + Vault-Bundle auf SSD und (optional) Offsite; Rechte-Test alle 30 Tage (WP-15).

**Betriebs-Checks:** `stratum_health` + `docker stats` täglich; `vmstat`-Warnung bei si/so > 0; Metrik-Alarm bei Reflect-Budget > 80 %.

---

## 16. Offene Annahmen — als „gegeben angenommen“ markiert

Die folgenden Punkte werden **als gegeben angenommen** und sind keine offenen Fragen; sie sind Teil der finalen Spezifikation:

1. **Gegeben angenommen:** Ein FreeLLMAPI-kompatibler Endpunkt (Chat + Embeddings, Dimension 1024) ist verfügbar und stellt ausreichendes Rate-Limit für Ingestion/Reflect bereit.
2. **Gegeben angenommen:** Der Raspberry Pi 4 (8 GB) bootet von microSD, betreibt aber Daten (Postgres-Datenverzeichnis, Git-Vault, Swapfile) auf einer dauerhaft angeschlossenen USB-3.0-SSD.
3. **Gegeben angenommen:** Erwartete Aktivdatenmenge pro Pi-Deployment: **≤ 150 000 Fakten** und **≤ 50 000 Dokument-Chunks** (Summe der Vektoren ≤ 200 000 bei Dimension 1024, fp32 ≈ 4 KB/Vektor → ≈ 0,8 GB Rohdaten + HNSW-Overhead ≈ ×1,5 → ≈ 1,2 GB, was in das 2-GB-DB-Limit passt). Für den Pi gilt: Embeddings werden als `halfvec(1024)` (fp16) gespeichert, das spart ~50 % Vektorspeicher. Größere Bestände (> 200 k Vektoren) sind dem Server-Profil vorbehalten; auf dem Pi wird bei Überschreitung eine **Off-Pi-Reindex-Routine** (Dump → HNSW-Build auf x86 → Restore) ausgelöst. Die ursprüngliche 5-Mio.-Annahme war mit 8 GB RAM unvereinbar und wurde verworfen.
4. **Gegeben angenommen:** Agenten (Claude Code, Cursor, Codex, Hermes) sprechen MCP entweder per stdio (lokal) oder per Streamable-HTTP (Server) an; ein MCP-fähiger Client ist vorhanden.
5. **Gegeben angenommen:** Die zram-Vorgabe wird als **1 GB, Priorität 100, ausschließlich für System- (Nicht-DB-)Pages** umgesetzt. Postgres wird per Service-Attribut **`mem_limit: 2g` + `memswap_limit: 2g`** (nicht über `deploy.resources`, das kein Swap-Feld kennt) der Swap (zram wie SSD) entzogen; damit ist `memory-swap == memory` real im Compose abgebildet (§10.2). Dies ist die pragmatischste Auslegung, die zram-Vorgabe und DB-Stabilität gleichzeitig erfüllt (siehe Risiko „Pi-Performance“).
6. **Gegeben angenommen:** Deutsch ist die Dokumentationssprache, Code/Identifier/Tool-Namen sind englisch.
7. **Gegeben angenommen:** Der Reflect-Zeitplan (täglich 03:00) wird in der Zeitzone des Deployment-Hosts interpretiert; alle Zeitstempel in der DB sind UTC.
8. **Gegeben angenommen:** Ein einzelner Tenant (`default`) für den Pi-Betrieb; Multi-Tenant ist im selben Code aktiv, aber erst im Server-Profil produktiv genutzt.
9. **Gegeben angenommen:** Benchmark-Datensätze (LoCoMo, LongMemEval) sind öffentlich beziehbar und dürfen für interne Evaluation genutzt werden.
10. **Gegeben angenommen:** Projektlizenz ist Apache-2.0; alle übernommenen Code-/Schema-Artefakte stammen aus Apache-2.0- oder MIT-Quellen.
