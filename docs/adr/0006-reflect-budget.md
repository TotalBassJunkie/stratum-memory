# ADR-0006: Reflect als kostenbegrenzter Batch

**Status:** accepted

## Kontext
Hindsight-Reflect ist rechen-/tokenintensiv. Ungebremster Reflect kann auf dem Pi Kosten und Laufzeit
sprengen.

## Entscheidung
- Reflect läuft als **Batch** (täglich 03:00 lokal, Trigger bei ≥ 500 `memory_events`, manuell).
- **Budget**: 20 000 Tokens/Lauf Standard, hart max. 50 000; Abbruch mit `BUDGET_EXCEEDED`.
- **Max. 1 Lauf** gleichzeitig (DB-Lock). Bevorzugt günstiges Modell, Chunk-Stichprobe.
- Ergebnisse sind **immer Kandidaten** (kein Direktschreiben).

## Konsequenzen
- Vorhersehbare Kosten; Synthese fehlerhaft → einfach nicht promoten.
