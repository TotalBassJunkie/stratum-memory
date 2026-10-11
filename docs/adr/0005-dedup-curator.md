# ADR-0005: Add-only-Dedup & Curator-Gate

**Status:** accepted

## Kontext
Add-only-Speicher (Mem0) neigt zu Dubletten; freies Schreiben aller Komponenten führt zu
„Memory-Verschmutzung".

## Entscheidung
- **Add-only-Dedup**: Ähnlichkeitssuche (Cosine) gegen aktive Fakten. Ist die Ähnlichkeit **≥ 0.92**,
  ist **ADD verboten** — der Klassifikator muss UPDATE/DELETE/NOOP wählen. ADD nur bei **< 0.92**.
- **Curator** ist die einzige Brücke Staging → Aktivbestand, mit 7 Kriterien (Dedup, Widerspruch,
  Konfidenz ≥ 0.6, Provenienz, Sicherheit/PII, Tenant, Zeitkonsistenz).
- **Nur der Curator** schreibt in Aktivtabellen; Reflect-Ergebnisse sind immer Kandidaten.
- Batches: max. `CURATOR_MAX_PROMOTIONS_PER_RUN` (Default 200) Promotionen/Lauf.

## Konsequenzen
- Kein Pfad schreibt ungeprüft in den Aktivbestand → niedrige Kontaminationsrate.
- Jede Entscheidung erzeugt einen `memory_events`-Eintrag (append-only Journal).
