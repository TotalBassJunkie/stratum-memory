# Stratum-Memory — Betriebshandbuch (WP-17)

Praxisleitfaden für den Dauerbetrieb von Stratum-Memory. Er ergänzt die
Tiefendokumente und wiederholt sie nicht:

| Thema | Detaildokument |
|---|---|
| Pi-Deployment (SSD, Swap, zram, Limits) | `deploy/pi/README.md` |
| Server-/Multi-Tenant-Profil (TLS, Caddy) | `deploy/server/README.md` |
| Backup, Restore, Retention, Drill | `docs/backup-restore.md` |
| MCP-Tools (Signaturen, Scopes, Fehlercodes) | `docs/api-reference.md` |
| Architektur & Entscheidungen | `MASTER-PLAN.md`, `docs/adr/` |

Rahmenbedingungen (unveränderlich): `MASTER-PLAN.md` §10 (Pi-Profil), §15
(Rollback/Betrieb), §16 (Annahmen). Drei Container — `stratum-db`,
`stratum-core`, `stratum-worker` — plus optional `stratum-caddy` (Server).

---

## 1. Betriebsmodell

- **Code** via Git (Tag = Release), **DB-Migrationen** als idempotente
  `init/*.sql`-Skripte (ein Alembic-basierter Migrations-Workflow ist **geplant**,
  nicht verfügbar), **Vault** (Markdown + Git) im Volume `kb`, **Container** via
  Compose.
- **DB-Rollen**: `stratum` = Owner/Superuser (nur Init/Migration/Dump),
  `stratum_app` = nicht-privilegierte App-Rolle (RLS-unterworfen). Core und Worker
  laufen **ausschließlich** als `stratum_app`.
- **Tenant-Isolation** per Row-Level-Security: `set_config('stratum.tenant', …, true)`
  pro Verbindung; die App-Rolle ist kein Tabellen-Owner, deshalb greift RLS.
- **Wahrheitsjournal**: `memory_events` ist append-only — jede Schreiboperation
  erzeugt genau einen Eintrag (`ADD|UPDATE|DELETE|NOOP|PROMOTE|ARCHIVE|FORGET|
  REFLECT`). Die App-Rolle darf darin nur lesen und einfügen (kein UPDATE/DELETE).

---

## 2. Deployment

### 2.1 Raspberry Pi 4 / 8 GB (Standardprofil)

Einmalige Provisionierung (idempotent) und Start — Details in
`deploy/pi/README.md`:

```bash
sudo bash deploy/pi/setup-pi.sh          # SSD-Layout, Docker, Swap/zram, Secrets-Platzhalter
# Secrets befüllen (Abschnitt 3 von deploy/pi/README.md)

docker compose \
  -f docker-compose.yml \
  -f deploy/pi/docker-compose.override.yml \
  up -d
```

Wichtig: Immer **beide** Compose-Dateien angeben. Der Pi-Override setzt die
RAM-Limits (`db 2g`, `core 1g`, `worker 1.5g`), hält die DB swapfrei
(`memswap_limit == mem_limit`), bindet `pgdata` und `kb` auf `/mnt/ssd/stratum/…`
und setzt `restart: unless-stopped`.

### 2.2 Server / Multi-Tenant

```bash
export STRATUM_DOMAIN=memory.example.com
docker compose \
  -f docker-compose.yml \
  -f deploy/server/docker-compose.override.yml \
  up -d
```

Der Server-Override schaltet `STRATUM_VECTOR_TYPE=vector` (vollpräzise, statt
`halfvec`), erhöht Postgres-RAM/Connections und ergänzt `stratum-caddy`
(TLS + Reverse-Proxy auf `stratum-core:8088`). Nur `/mcp` ist öffentlich.

> **Vektorprofil-Wechsel** (`halfvec` ⇄ `vector`) ist **kein** Live-Flag: die
> Spalte ändert sich, bestehende Vektoren müssen neu eingebettet werden. Wie ein
> Re-Index behandeln, nicht wie eine Umschaltung.

### 2.3 Lokaler Entwickler-/stdio-Modus

Ein residenter Container kann kein stdio anbieten. Für stdio-Clients den Server
client-gestartet betreiben:

```bash
# nativ
python -m stratum.mcp --transport stdio
# oder im Compose-Netz (DNS stratum-db funktioniert)
docker compose run --rm -i stratum-core python -m stratum.mcp --transport stdio
```

### 2.4 MCP-Client anbinden

- **Streamable-HTTP** (Pi/Server resident): Client-URL `http://127.0.0.1:8088/mcp`
  (Pi) bzw. `https://$STRATUM_DOMAIN/mcp` (Server). Der Container bindet intern an
  `0.0.0.0:8088`; die localhost-Beschränkung erfolgt host-seitig über das
  Port-Mapping `127.0.0.1:8088:8088`.
- **stdio** (lokal): den Client so konfigurieren, dass er
  `python -m stratum.mcp --transport stdio` startet.
- Die zehn Tools und ihre Scopes: `docs/api-reference.md`.

---

## 3. Health-Checks

### 3.1 Täglicher Basis-Check

```bash
# Container-Status
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml ps

# DB erreichbar
docker compose … exec stratum-db pg_isready -U stratum -d stratum

# MCP-Readiness (über den Core)
curl -sS http://127.0.0.1:8088/health
# {"status":"ok","version":"0.1.0","db":{"ok":true,"error":null},
#  "jobs":{"queued":0,"error":null},"git":{"ok":true,"path":"…"},
#  "llm":{"ok":true,"base_url":"…"}}   # HTTP 200 bei status="ok", sonst 503
```

Der Endpunkt gibt den rohen Observability-Bericht aus
(`stratum.observability.health`); das Tool `stratum_health` liefert dieselben
Felder flach (`db`/`git`/`llm` als Bool, `queued_jobs`).

`stratum_health` liefert `status="ok"` genau dann, wenn die DB erreichbar ist,
sonst `"degraded"` (nie eine Exception). Ebenso per CLI:

```bash
python -m stratum.cli health      # bzw. das Console-Script `stratum health`
```

Der Exit-Code ist 0 bei `ok`, sonst 1 — geeignet für Monitoring-Proben.

### 3.2 Speicher-/Swap-Check (Pi)

```bash
docker stats --no-stream
vmstat 1 5                 # si/so sollte ≈ 0 sein
docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' stratum-db
# beide == 2147483648 (2 GiB)  => DB swapfrei
```

Erwartungswerte und Ursachenanalyse: `deploy/pi/README.md` §6/§7.

### 3.3 Metriken

`/metrics` liefert Prometheus-Textformat (Zähler, Gauges, Summary-Familien) sowie
Token-/Kosten-Zähler je Tenant/Modell (`observability.Metrics`). Alarm-Schwelle:
Reflect-Budget > 80 % des Laufs.

---

## 4. Logs

- **Strukturierte JSON-Logs** (ein Objekt pro Zeile): `time`, `level`, `logger`,
  `msg` plus Felder. Konfiguration via `stratum.observability.setup_logging`.
- **Container-Logs**:

  ```bash
  docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml \
    logs -f --tail=200 stratum-core stratum-worker
  ```

- **Tool-Fehler** landen mit voller Detailursache im Log (`tool <name> failed`);
  der Client erhält nur die generische `INTERNAL`-Meldung.
- **Journal-Auswertung** (append-only Wahrheit):

  ```sql
  SELECT created_at, op, target_uri, actor
    FROM memory_events
   WHERE tenant_id = 'default'
   ORDER BY created_at DESC LIMIT 50;
  ```

- **Secrets** erscheinen nie in Logs (keine Secrets in Env-Dateien/Logs,
  MASTER-PLAN §11).

---

## 5. Backup / Restore

Vollständig dokumentiert in `docs/backup-restore.md` (WP-15). Kurzfassung:

- **Zielwerte**: RPO ≤ 24 h, RTO ≤ 1 h, 14 Archive je Familie, Restore-Drill alle
  30 Tage.
- **Täglich** (Cron/systemd-Timer, 03:00 UTC):

  ```bash
  ./scripts/backup.sh                 # Pi/Linux
  .\scripts\backup.ps1                # Windows-Entwicklung
  ```

  Es entstehen zwei Archive je Lauf: `stratum-<stamp>.dump` (DB) und
  `vault-<stamp>.tar.gz` (Git-Vault).
- **Restore/Verify**:

  ```bash
  ./scripts/restore.sh --dump <dump> --verify
  ./scripts/restore.sh --dump <dump> --target-db stratum_restore_drill --force
  ./scripts/restore.sh --vault <vault.tar.gz> --target-dir /tmp/kb-restored
  ```

- **Drill-Abnahme**: `--verify` Exit 0, Restore < 1 h, Kerntabellen > 0,
  `git fsck --full` fehlerfrei.

---

## 6. Reflect-Betrieb

Reflect verdichtet Fakten/Episoden zu Synthesen und Skill-Vorschlägen — **immer
nur als Kandidaten** (`status='pending'`), nie direkt in den Aktivbestand. Die
Promotion macht ausschließlich der Curator.

- **Trigger** (§9):
  1. Zeitplan täglich 03:00 UTC (poll-basierter Scheduler-Tick im Worker),
  2. Bedarfs-Trigger `count(memory_events seit letztem Reflect) ≥ 500`,
  3. manuell via MCP `stratum_reflect` oder Job-Enqueue.
- **Kosten-/Nebenläufigkeitsschranken**: Budget `reflect_budget_tokens`
  (Default 20 000), hart auf `reflect_max_tokens` (Default 50 000) geklemmt;
  Abbruch bei Überschreitung schreibt `BUDGET_EXCEEDED`. Max. **ein** Lauf
  gleichzeitig (Postgres-Advisory-Lock je Tenant); Timeout 30 min (`TIMEOUT_EXCEEDED`).
- **Manuell anstoßen**:

  ```bash
  python -m stratum.worker --enqueue reflect --payload '{"scope":"default","budget_tokens":20000}'
  ```

- **Ergebnisse prüfen**:

  ```sql
  SELECT kind, status, source_uri, created_at
    FROM staging_candidates
   WHERE tenant_id = 'default' AND kind IN ('synthesis','skill')
   ORDER BY created_at DESC LIMIT 20;
  ```

  Anschließend über `stratum_promote` (Scope `promote`) freigeben.

---

## 7. Rollback (MASTER-PLAN §15)

| Szenario | Sofortmaßnahme | Rollback |
|---|---|---|
| Fehlerhafte Migration | Core/Worker stoppen | Schema-Änderungen laufen über idempotente `init/*.sql`; kein Downgrade einzelner Migrationen — Restore aus letztem `pg_dump` (§5 Backup) |
| Vergifteter Kandidat promoviert | `stratum_archive(uri)` + `memory_events` prüfen | Fakt aus `memory_events.before` rekonstruieren |
| Reflect läuft Amok (Kosten) | Worker stoppen, Job-Lock freigeben | Budget erst nach Root-Cause erhöhen |
| LLM-API-Ausfall | Degradationsmodus (nur Lesen); Schreiben bleibt `pending` | Nach API-Rückkehr `pending`-Queue abarbeiten |
| OOM-Kill auf Pi | Restart-Policy greift | Limits/Bestand prüfen, `shared_buffers` senken |
| Datenkorruption | DB read-only | Restore aus letztem `pg_dump` (RPO ≤ 24 h, RTO ≤ 1 h) |
| Release-Regression | — | `stratum-core`/`stratum-worker` auf Vor-Tag zurückrollen (Image-Pin) |

**Release-Rollback konkret** (GitOps, Tag = Release):

```bash
git checkout <vorheriger-tag>
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml \
  up -d --build stratum-core stratum-worker
```

**Migration-Rollback**: Schema-Änderungen laufen über idempotente
`init/*.sql`-Skripte; ein Downgrade einzelner Migrationen existiert nicht.
Rollback = Restore aus dem letzten `pg_dump` (siehe §5 Backup; erst in eine
Wegwerf-DB prüfen, dann produktiv einspielen).

**Worker stoppen** (ohne die DB anzuhalten):

```bash
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml stop stratum-worker
```

---

## 8. Troubleshooting

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `stratum_health` → `degraded` | DB nicht erreichbar | `docker compose ps`; `pg_isready`; DB-Logs |
| `FORBIDDEN` bei einem Tool | fehlender Scope oder Cross-Tenant-URI | Key-Scopes prüfen (`docs/api-reference.md`) |
| Kandidat bleibt `pending` | Curator noch nicht gelaufen | `stratum_promote` bzw. Worker/`promote_batch` |
| `promote` → `rejected` `duplicate` | Fakt existiert schon | erwartetes Verhalten (Add-only-Dedup, §8) |
| Reflect staged nichts | Budget aufgebraucht / keine neuen Fakten | Budget prüfen, `BUDGET_EXCEEDED` im Journal |
| `BUDGET_EXCEEDED` bei `stratum_reflect` | `budget_tokens` > `reflect_max_tokens` | Budget senken oder Limit konfigurieren |
| Container kommt nicht hoch (`EACCES` auf `kb`) | falsches Ownership am Bind-Mount | `chown -R 10001:10001 /mnt/ssd/stratum/kb` |
| Container kommt nicht hoch (`EACCES` auf `pgdata`) | falsches Ownership | `chown -R 999:999 /mnt/ssd/stratum/pgdata` |
| Pi „hängt“, `vmstat` si/so > 0 | Swap zu aktiv / DB swappt | DB-Swapfreiheit prüfen; `swap-setup.sh` erneut |
| Backup-Exit 5 `docker not found` | Docker fehlt | Docker-Daemon starten (`backup.sh`, Pi/Linux). Nur in den Windows-PowerShell-Skripten (`backup.ps1`/`restore.ps1`) kann zusätzlich `STRATUM_DOCKER` auf den `docker`-Pfad gesetzt werden |
| Nach Reboot Swap weg | fstab/Timer fehlt | `swap-setup.sh`; `systemctl status zramswap` |

Vertiefung: `deploy/pi/README.md` §7, `deploy/server/README.md`,
`docs/backup-restore.md` §9.

---

## 9. Betriebs-Checkliste (täglich / wöchentlich / monatlich)

**Täglich**
- `stratum_health` + `docker compose ps`
- `docker stats --no-stream` (Budget einhalten)
- `vmstat` (si/so ≈ 0)
- Backup-Lauf erfolgreich? (`backup.log`)

**Wöchentlich**
- Reflect-Integritätsbericht: Widerspruchs- und Dublettenquote (`memory_events`,
  `staging_candidates`)
- Bestandsgröße vs. Pi-Zielmenge (≤ 150 k Fakten, ≤ 50 k Chunks, ≤ 200 k Vektoren)

**Monatlich**
- Restore-Drill (`docs/backup-restore.md` §6) + Ergebnis ins Betriebslog
- Metrik-Alarm geprüft (Reflect-Budget > 80 %)
- Container-Images/Abhängigkeiten auf Sicherheitsupdates geprüft