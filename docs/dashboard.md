# Stratum-Memory — Operator-Dashboard

Ein kleines, **dependency-freies** Bedien- und Einblick-Panel über den laufenden
Store. Es wird vom MCP-HTTP-Transport mitgeliefert und ist als einzelne,
selbst-enthaltene HTML-Seite (eingebettetes CSS/JS, **kein CDN, keine externen
Fonts, kein Build-Schritt**) in `src/stratum/dashboard.py` implementiert — damit
läuft es offline auf dem Raspberry Pi ohne Node-Toolchain.

## Zweck

Read-mostly Single-Operator-Oberfläche: Health, Zähler, Bereiche, Browse/Reader
(L0/L1/L2), Suche, Staging-Gate, Event-Journal und zwei Wartungsaktionen
(`promote`, `reflect`). Sie greift **read-mostly** mit RLS-Bypass auf denselben
Postgres-Store zu wie der MCP-Server.

Design: dark instrument-panel, Akzent **Amber/Cyan** aus dem Logo, dichte/ruhige
Flächen, Monospace für Zahlen, `prefers-reduced-motion` wird respektiert, keine
Emojis, WCAG-AA-Kontrast (Body-Text ≥ 4.5:1).

## URL & Port

| Endpunkt | Zweck | Auth |
|---|---|---|
| `http://127.0.0.1:8088/dashboard` | die UI (Single-Page) | offen (nur HTML-Hülle) |
| `http://127.0.0.1:8088/` | wie `/dashboard` | offen |
| `http://127.0.0.1:8088/health` | Health-Probe (JSON) | offen |
| `http://127.0.0.1:8088/metrics` | Metriken | offen |
| `/dashboard/api/*` | JSON-API (siehe unten) | durch MCP-Auth geschützt |

Der Prozess bindet im Container `0.0.0.0:8088`; die Host-Exposition ist per
Port-Mapping (`127.0.0.1:8088:8088` im Pi-Profil) auf localhost beschränkt. Auf
dem Server steht Caddy/TLS davor (`https://$STRATUM_DOMAIN/dashboard`).

## Auth (Key setzen)

Im **Standardbetrieb** läuft der Server mit transparentem Bootstrap-Kontext
(`admin`, Tenant `default`) — kein Key nötig. Ist
`STRATUM_REQUIRE_API_KEY=1` gesetzt, verlangen die `/dashboard/api/*`-Routen
`Authorization: Bearer stk_<tenant>_<random>`:

- alle **GET**-Routen brauchen den Scope **`read`**,
- die beiden **POST**-Routen (`promote`, `reflect`) brauchen **`promote`**
  (`admin` impliziert alle Scopes).

Die HTML-Hülle, `/health` und `/metrics` bleiben absichtlich offen, damit der
Browser überhaupt laden und nach dem Token fragen kann.

**Key in der UI setzen:** Button `key` in der Kopfleiste (oder automatisch, wenn
ein `AUTH`-Fehler auftritt). Der eingegebene Bearer-Token wird in
`localStorage.stratum_token` gespeichert; die Ansicht lädt nach dem Speichern
neu. Ein leeres Feld bedeutet „lokal / kein Key" und entfernt den Token.

## JSON-API (mit `curl`)

Ausgangspunkt; `-H "Authorization: Bearer stk_…"` nur anhängen, wenn das
Key-Gate aktiv ist.

```bash
BASE=http://127.0.0.1:8088
# KEY=stk_default_xxx   # optional
AUTH=(-H "Authorization: Bearer $KEY")   # weglassen, wenn kein Gate
```

### `GET /dashboard/api/overview`
Health, Zähler (files/chunks/facts/entities/events/staging_pending/jobs) und die
letzten 12 Events.

```bash
curl -s "${AUTH[@]}" "$BASE/dashboard/api/overview" | jq .
```

### `GET /dashboard/api/areas`
Top-Level-Bereiche (`split_part(uri,'/',3)`) mit Zählern, absteigend.

```bash
curl -s "${AUTH[@]}" "$BASE/dashboard/api/areas" | jq '.areas'
```

### `GET /dashboard/api/list`
Memory-Dateien auflisten. Parameter: `prefix` (optional), `limit` (1–500,
Default 100).

```bash
curl -s "${AUTH[@]}" "$BASE/dashboard/api/list?limit=20" | jq '.items'
curl -s "${AUTH[@]}" \
  "$BASE/dashboard/api/list?prefix=stratum://docs/default/openviking/memories&limit=50" | jq .
```

### `GET /dashboard/api/get`
Eine URI lesen. Parameter: `uri` (Pflicht), `level` = `l0` | `l1` (Default) |
`l2`. Findet die URI keine Datei, werden stattdessen bis zu 50 **Fakten** mit
dieser `source_uri` geliefert (`kind: "facts"`); sonst `{"error":"not_found"}`.

```bash
curl -s "${AUTH[@]}" \
  "$BASE/dashboard/api/get?uri=stratum://docs/default/openviking/memories/foo.md&level=l1" | jq .
```

### `GET /dashboard/api/search`
Hybrid/Vector/Graph/KV-Abfrage (benötigt das Embedding-Backend). Parameter:
`q`, `mode` (`hybrid`|`vector`|`graph`|`kv`), `k` (Default 8), `level`,
`scope` (optional). Fehler kommen als `{"error": …,"results":[]}` mit
HTTP 200 zurück, damit die UI sie anzeigen kann.

```bash
curl -s "${AUTH[@]}" \
  "$BASE/dashboard/api/search?q=darkmatter&mode=hybrid&k=5" | jq '.results'
```

### `GET /dashboard/api/staging`
Staged candidates. Parameter: `status` = `pending` (Default) | `approved` |
`rejected`, `limit` (1–200, Default 50).

```bash
curl -s "${AUTH[@]}" "$BASE/dashboard/api/staging?status=pending" | jq '.items'
```

### `GET /dashboard/api/events`
Das append-only `memory_events`-Journal. Parameter: `limit` (1–500, Default 50).

```bash
curl -s "${AUTH[@]}" "$BASE/dashboard/api/events?limit=100" | jq '.items'
```

### `POST /dashboard/api/promote`  *(Scope: promote)*
Einen Staging-Kandidaten über den Curator freigeben. Der Vorgang wird mit
`actor = "dashboard"` im `memory_events`-Journal attribuiert.

```bash
curl -s "${AUTH[@]}" -X POST "$BASE/dashboard/api/promote" \
  -H 'Content-Type: application/json' \
  -d '{"candidate_id":"<uuid>"}' | jq .
```

### `POST /dashboard/api/reflect`  *(Scope: promote)*
Einen budgetierten Reflect-Lauf einreihen. Body optional/leer. Optionale Felder:
`budget_tokens` (Default `Settings.reflect_budget_tokens`, auf
`Settings.reflect_max_tokens` begrenzt) und `scope`.

```bash
curl -s "${AUTH[@]}" -X POST "$BASE/dashboard/api/reflect" \
  -H 'Content-Type: application/json' -d '{"budget_tokens":20000}' | jq .
```

## Screenshot-Beschreibung

Dunkles Instrumenten-Panel. Links eine schmale Navigationsspalte
(Overview · Search · Browse · Staging · Events · Operations), oben eine
Kopfleiste mit Logo, Status-Pill (Punkt grün = `healthy`, rot = degraded),
Tenant-Pill, Queue-Zähler sowie den Buttons `key` und `refresh`. Der
Overview-Bereich zeigt eine Reihe von **KPI-Kacheln** (Files, Chunks, Facts,
Entities, Staging, Events, große Monospace-Zahlen) und darunter die Tabelle
„Recent activity" mit farbigen Operations-Tags (ADD grün, FORGET/DELETE rot,
UPDATE/PROMOTE amber, sonst cyan). Browse und Staging zeigen Tabellen in einem
horizontal scrollbaren Wrapper; ein Klick auf eine URI öffnet den Reader mit
den Reitern L0/L1/L2. Unten rechts erscheinen Toasts.

## Troubleshooting

- **Leere Ansichten** („Nothing found", „No events", „No candidates"):
  meist der **falsche Tenant** (Key-Tenant ≠ erwarteter Tenant) oder es liegen
  noch **keine Daten** im Store vor (kein `var/`-Vault befüllt, keine
  Ingestion gelaufen). Prüfe Tenant/Prefix und ob die Zähler im Overview > 0
  sind. `areas` und `list` lesen die Datenbank, nicht das Dateisystem — ein
  leeres `var/` erklärt fehlende Chunks/Fakten, nicht fehlende Dateizeilen.
- **`AUTH`-Fehler / alles 401/403**: Das Key-Gate ist aktiv, aber es fehlt/ist
  falsch der Bearer-Token. In der UI den `key`-Button nutzen (der Dialog wird bei
  einem `AUTH`-Fehler automatisch geöffnet und die Ansicht lädt danach neu).
  Per `curl` `-H "Authorization: Bearer stk_…"` mitschicken; für
  `promote`/`reflect` braucht der Key den Scope `promote`.
- **420/422-verbotene Suche**: `search` meldet `{"error": …}` mit HTTP 200, wenn
  das Embedding-Backend fehlt — dann `mode=graph`/`kv` versuchen oder ein
  lokales Modell konfigurieren.
- **Tabellen laufen auf schmalen Screens über**: Der Tabellen-Wrapper
  (`.tw`, `overflow:auto`) fängt das ab; falls doch, Browser-Zoom prüfen.
- **Kein CDN / offline**: Bewusst — die Seite lädt keinerlei externe Assets.
