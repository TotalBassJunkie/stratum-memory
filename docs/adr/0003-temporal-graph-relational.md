# ADR-0003: Bi-temporaler Graph relational

**Status:** accepted

## Kontext
Zep/Graphiti zeigen den Nutzen bi-temporaler Graphen, benötigen aber eine eigene Graph-DB (hohe
Latenz, eigener Betrieb). Auf dem Pi ist ein JVM-/Graph-DB-Stack nicht vertretbar.

## Entscheidung
Der bi-temporale Graph wird **relational in PostgreSQL** abgebildet: Tabellen `entities`, `facts`,
`edges`. Traversierung über rekursive CTEs. Gültigkeitsachse `valid_from/to`, Transaktionsachse
`tx_from/to`. Es wird keine Graph-DB eingesetzt.

## Konsequenzen
- Eine Datenbank für alles; deutlich geringerer Betriebs- und RAM-Aufwand.
- Traversierungen sind SQL-CTEs statt Cypher; für die Pi-Zielmenge (≤ 150k Fakten) ausreichend.
- Server-Skalierung über Indizes und ggf. Materialisierung.
