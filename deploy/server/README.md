# Server-Profil (Multi-Tenant) — `deploy/server/`

Dieses Verzeichnis enthält das **Server-Overlay** für den Multi-Tenant-Betrieb von
Stratum-Memory (MASTER-PLAN §10.5). Es ergänzt die Pi-Basis (`docker-compose.yml`)
und ersetzt sie **nicht**.

| Datei | Zweck |
|---|---|
| `docker-compose.override.yml` | Server-Tuning: `vector(1024)` statt `halfvec`, größere Postgres-Werte, `stratum-caddy`-Service |
| `Caddyfile` | TLS-Terminierung + Reverse-Proxy auf `stratum-core:8088` |
| `README.md` | Dieser Betriebsleitfaden |

## Start

```bash
# Domain setzen (öffentlich auflösender A/AAAA-Record auf diesen Host):
export STRATUM_DOMAIN=memory.example.com

docker compose \
  -f docker-compose.yml \
  -f deploy/server/docker-compose.override.yml \
  up -d
```

Auf Ports **80** (ACME-Challenge + Redirect) und **443** (TLS) muss der Host von
außen erreichbar sein, damit Caddy ein Let's-Encrypt-Zertifikat beziehen kann.

Nur den MCP-Endpunkt braucht man öffentlich; `stratum-db` bleibt unveröffentlicht,
`stratum-core:8088` bleibt host-seitig auf `127.0.0.1` gebunden (siehe Basis).

## Unterschiede zum Pi-Profil

* **Vektortyp** — `stratum-core` läuft mit `STRATUM_VECTOR_TYPE=vector`. Der Server
  speichert `vector(1024)` (vollpräzise); der Pi nutzt `halfvec(1024)` (fp16,
  halber Platz). Siehe MASTER-PLAN §5.2/§10.5. `stratum-db` setzt denselben Wert,
  weil das Schema-Rendering (`init/003_schema.sh`) den Spaltentyp beim
  **ersten** Init aus genau dieser Variable zieht — ohne sie entstünde
  `halfvec(1024)`, während Core/Worker `vector` hineinschreiben würden.
* **Postgres-RAM** — `shared_buffers=4GB`, `effective_cache_size=12GB`,
  `max_connections=100`. Die DB bleibt swapfrei (`memswap_limit == mem_limit`).
* **TLS** — `stratum-caddy` terminiert HTTPS und ist der einzige öffentlich
  exponierte Dienst; er proxyt auf `stratum-core:8088` im Compose-Netz.

> **Achtung Migration Pi ↔ Server:** Ein Wechsel des Vektortyps ist **kein**
> Live-Umschalten. Bestehende Vektoren müssen neu eingebettet werden (Spaltentyp
> ändert sich `halfvec(1024)` ⇄ `vector(1024)`). Für den Pi gilt: Reindex
> off-Pi durchführen (> 200k Vektoren, §10). Behandle den Wechsel wie einen
> Re-Index, nicht wie ein Flag.

## Multi-Tenant-Betrieb

Isolation erfolgt über Row-Level-Security, nicht über getrennte Datenbanken:

* **App-Rolle** — Core/Worker verbinden als `stratum_app` (kein Superuser, kein
  `BYPASSRLS`, kein Tabellen-Owner). Genau deshalb greift RLS überhaupt.
* **Tenant pro Verbindung** — bei jedem Verbindungsaufbau setzt der Code
  `set_config('stratum.tenant', <tenant>, true)` (transaktionslokal).
* **Auth** — API-Keys `stk_<tenant>_<random>`, nur als Argon2id-Hash gespeichert;
  Scopes `read|write|promote|admin`. `admin` darf tenant-übergreifend arbeiten.
  Das Server-Overlay setzt `STRATUM_REQUIRE_API_KEY=1` (Default), damit der über
  Caddy öffentlich erreichbare `/mcp`-Endpunkt **immer** ein
  `Authorization: Bearer stk_...` verlangt. Nur für lokales Debugging auf `0`
  setzen (`STRATUM_REQUIRE_API_KEY=0`) — nie auf einem öffentlich erreichbaren
  Host. Die Probes `/health` und `/metrics` bleiben immer öffentlich.
* **DB nie öffentlich** — der DB-Port bleibt auf dem internen Docker-Netz.

Ein Mandant entspricht dem zweiten URI-Segment (`stratum://docs/<tenant>/…`,
§5.1). Für saubere Trennung je Mandant gibt es eine eigene App-Rolle pro Tenant
oder mindestens einen eigenen API-Key je Mandant.

## TLS & Sicherheit

* **HSTS** — der Caddyfile setzt `Strict-Transport-Security` (1 Jahr,
  `includeSubDomains`).
* **Nur `/mcp` exponiert** — alle anderen Pfade antworten mit `404`, damit keine
  versehentlich registrierten Routen erreichbar sind. `/healthz` wird an den
  echten Core-Endpunkt `GET /health` durchgereicht (DB-/Queue-/LLM-Zustand),
  nicht mehr statisch beantwortet.
* **API-Key-Gate** — `/mcp` verlangt bei gesetztem `STRATUM_REQUIRE_API_KEY`
  (Server-Default `1`) ein Bearer-Token; `/health` und `/metrics` bleiben frei.
* **Header** — `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, restriktive CSP.
* **Secrets** — Passwörter/Keys kommen ausschließlich aus Docker-Secrets
  (`*_FILE`-Variablen). **Keine Secrets in `docker-compose.override.yml`,
  `Caddyfile` oder dieser Datei.** Das TLS-Zertifikat ist kein Secret; Caddy
  holt es selbst über ACME.
* **Domain** — der Caddyfile enthält nur einen reservierten Platzhalter-Default
  (`.invalid`, RFC 2606). Die echte Domain wird über `STRATUM_DOMAIN` gesetzt.

## Backup (verbindlich: externe Postgres-Backups)

Postgres-Backups laufen **extern** über `scripts/backup.sh` / `backup.ps1`
(WP-15) — nicht über das Overlay, nicht auf derselben Maschine:

```bash
# Dump der Superuser-Rolle 'stratum' (besitzt alle Objekte), DB 'stratum':
docker compose -f docker-compose.yml \
  -f deploy/server/docker-compose.override.yml \
  exec -T stratum-db pg_dump -U stratum -Fc stratum > stratum-$(date +%Y%m%d-%H%M%S).dump
```

* **Warum extern?** microSD/Container-Volume ist kein Backup. Dumps und der
  `kb`-Vault gehören auf ein **anderes** Medium/Host (Retention wie in WP-15
  dokumentiert).
* **Restore** ist nur gegen eine passende pgvector-Version sinnvoll
  (`pgvector/pgvector:0.8.6-pg18`).
* Da das Serverprofil `vector(1024)` benutzt, sichere mit, welches Vektorprofil
  der Dump stammt (`halfvec` = Pi, `vector` = Server) — ein Restore über Profile
  hinweg erfordert einen Re-Index.

## Skalierung

Die Container sind einzeln skalierbar; die DB bleibt der Flaschenhals:

1. **Core horizontal** — `stratum-core` ist zustandslos (Zustand liegt in DB +
   Vault) und kann hinter Caddy mehrfach laufen. Caddy verteilt per
   Round-Robin auf alle Instanzen desselben Dienstnamens im Compose-Netz.
2. **Worker horizontal** — `stratum-worker` nutzt eine Job-Queue mit
   `SELECT … FOR UPDATE SKIP LOCKED` (WP-05b); mehrere Worker beanspruchen
   Jobs ohne Doppelabarbeitung.
3. **DB vertikal zuerst** — bevor horizontal skaliert wird, `shared_buffers`
   und `effective_cache_size` an den verfügbaren RAM anpassen.
4. **PgBouncer (optional)** — bei vielen kurzlebigen MCP-Verbindungen
   (`max_connections=100` als Engpass) einen Connection-Pooler vorschalten; ein
   vorbereiteter, auskommentierter Block liegt in
   `docker-compose.override.yml`. Im `transaction`-Pooling bleiben `SET LOCAL`/
   `set_config(..., true)` korrekt pro Transaktion gesetzt.
5. **Caddy** — bei mehreren Hosts/Regionen die Annahme-Schicht weiter nach vorn
   ziehen (Lastbalancer); TLS-Verwaltung bleibt bei Caddy.

## Betrieb prüfen

```bash
# Öffentlicher MCP-Endpunkt (erwartet 200/406 je nach Accept-Header, kein 404):
curl -sS -o /dev/null -w '%{http_code}\n' https://$STRATUM_DOMAIN/mcp

# Health-Probe über TLS (durchgereicht an den echten Core-Probe):
curl -sS https://$STRATUM_DOMAIN/healthz   # -> JSON-Statusreport (status=ok|degraded)

# DB-Verbindungslimit im Serverprofil:
docker compose exec stratum-db psql -U stratum -c 'SHOW max_connections;'  # -> 100
```

## Tests

`tests/test_deploy_server.py` prüft die Artefakte DB-frei: Overlay-YAML parsebar,
`STRATUM_VECTOR_TYPE=vector` gesetzt, `stratum-caddy` mit Ports 80/443 und
Caddyfile-Mount vorhanden, Caddyfile enthält `reverse_proxy` auf
`stratum-core:8088`, und es sind **keine Secrets** in den Dateien.
