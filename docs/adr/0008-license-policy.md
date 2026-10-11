# ADR-0008: Lizenzpolitik & Reuse-Grenzen

**Status:** accepted

## Kontext
Stratum-Memory übernimmt Konzepte aus mehreren Projekten, darunter AGPL-3.0 (OpenViking) und ein
unlizenziertes Repo (jeremykamber/strata-memory).

## Entscheidung
- Projektlizenz: **Apache-2.0**.
- **Nur Konzeptübernahme** (kein Code, keine Dateien) aus: OpenViking (AGPL-3.0),
  jeremykamber/strata-memory (keine Lizenz).
- **Code-/Schemaübernahme** nur aus Apache-2.0/MIT: mem0 (Apache-2.0), MCP-servers (Apache-2.0/MIT),
  graphiti (Apache-2.0), cognee (Apache-2.0), hindsight (MIT), pgvector (PostgreSQL/BSD-artig).

## Konsequenzen
- Keine AGPL-Verseuchung; Compliance-konform mit Apache-2.0.
