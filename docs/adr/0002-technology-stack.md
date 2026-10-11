# ADR-0002: Technologie-Stack

**Status:** accepted

## Kontext
Stratum-Memory muss vier Speicherebenen (Filesystem, bi-temporaler Graph, Vector/KV, Reflect)
auf einem Raspberry Pi 4 (8 GB) UND auf einem Multi-Tenant-Server betreiben. Externe LLM-Nutzung,
MCP-Integration, Apache-2.0-Lizenz.

## Entscheidung
- Sprache: **Python 3.12** (Ökosystem, MCP/FastMCP, LLM-Libs).
- Datenbank: **PostgreSQL 18 + pgvector 0.8** als einzige Stateful-Komponente.
- Queue: Postgres-`jobs`-Tabelle mit `SELECT ... FOR UPDATE SKIP LOCKED` (kein Redis).
- MCP: **FastMCP**; HTTP resident, stdio client-gestartet.
- Embeddings: `halfvec(1024)` (Pi) / `vector(1024)` (Server); extern erzeugt.
- Vault: lokales Git-Repo für Markdown.

## Konsequenzen
- Nur ein Container mit persistentem Zustand → geringer RAM-Bedarf auf dem Pi.
- Kein Redis/RabbitMQ, kein Neo4j/Qdrant.
- Spaltentyp je Deployment parametrisiert (`STRATUM_VECTOR_TYPE`); Restore konvertiert explizit.

## Alternativen (verworfen)
- Neo4j/FalkorDB (RAM), Qdrant/Weaviate (Extra-Dienst), SQLite (kein RLS/HNSW), Redis (unnötig),
  lokale GPU-Inferenz (Ressourcen).
