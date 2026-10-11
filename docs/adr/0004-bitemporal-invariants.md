# ADR-0004: Bi-temporale Invarianten & close-before-insert

**Status:** accepted

## Kontext
Bei jeder Korrektur eines Fakts muss der alte Fakt geschlossen und ein neuer eingefügt werden. Ein
partieller Unique-**Index** in PostgreSQL ist **nicht** `DEFERRABLE`, und Unique-Bedingungen werden
auf **Statement**-Ebene geprüft.

## Entscheidung
1. Pro `(tenant_id, subject_entity_id, predicate)` existiert **höchstens ein offener Fakt zum
   Schreibzeitpunkt** (`valid_to IS NULL AND tx_to IS NULL`), erzwungen durch partiellen
   Unique-Index `facts_one_open_per_pred_idx`.
2. Verbindliche Reihenfolge **close-before-insert**: erst `UPDATE` (alten Fakt schließen, ggf.
   `superseded_by`), dann `INSERT` (neuen Fakt) — in einer Transaktion.
3. Zukunftsbefristete Fakten sind ein anderer `predicate`-Slot (z.B. `plan_next`) oder schließen den
   aktuell offenen Fakt explizit.

## Konsequenzen
- Korrekturen sind transaktional konsistent und verlieren keine Historie.
- Ein umgekehrtes INSERT→UPDATE bricht die Transaktion ab → Tests erzwingen die Reihenfolge.
