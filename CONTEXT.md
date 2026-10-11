# CONTEXT — Ubiquitous Language (Stratum-Memory)

## Kernbegriffe

- **stratum://-URI**: kanonischer Bezeichner `stratum://<bereich>/<tenant>/<pfad>`; erste Klasse
  im System. `<bereich>` ∈ {docs, facts, entities, episodes, skills, syntheses}.
- **Tenant**: Isolationsdomäne; zweites URI-Segment; Default `default`. Durchgesetzt per RLS.
- **Ebene/Layer**: eine der vier Speicherschichten — Filesystem, Temporal-Graph, Vector/KV, Reflect.
- **L0/L1/L2**: Ladestufen eines Dokuments (Abstraktion / Kurzfassung / Volltext).
- **Fakt**: bi-temporale Aussage `(subject, predicate, object)` mit `valid_from/to` (Weltzeit) und
  `tx_from/to` (Systemzeit).
- **Offener Fakt**: Fakt mit `valid_to IS NULL AND tx_to IS NULL`.
- **close-before-insert**: verbindliche Statement-Reihenfolge beim Korrigieren eines Fakts —
  erst den alten Fakt schließen, dann den neuen einfügen (partieller Unique-Index, nicht deferrable).
- **Kandidat (staging candidate)**: noch nicht promovierte Aussage/Dokument/Entity im Staging.
- **Curator**: Gate, das Kandidaten gegen 7 Kriterien prüft und promoviert oder ablehnt.
- **Promotion**: Überführung eines Kandidaten in den Aktivbestand (Tabellen + Filesystem + Event).
- **Add-only-Dedup**: Ähnlichkeitsbasierte Entscheidung ADD/UPDATE/DELETE/NOOP; ADD nur bei
  Cosine-Ähnlichkeit < 0.92.
- **memory_events**: append-only Wahrheitsjournal jeder Speicheroperation.
- **Reflect**: periodischer, kostenbegrenzter Batch, der Fakten zu Synthesen verdichtet; Ergebnisse
  sind immer Kandidaten.
- **ECL**: Extract → Cognify → Load, die Ingestion-Pipeline.
- **App-Rolle**: nicht-privilegierte DB-Rolle (`stratum_app`), der RLS unterworfen; Core/Worker nutzen
  ausschließlich sie.

## Nicht-Ziele

- Kein Agent-Runtime (Letta-artig); Stratum liefert nur Memory/Context.
- Keine lokale GPU-Inferenz; LLM/Embeddings ausschließlich über externe API.
- Keine eigene Graph-DB; bi-temporaler Graph relational in PostgreSQL.
