# Backup, Restore & Migrationen (WP-15)

Betriebswerkzeuge für die Stratum-Memory-Datenhaltung: **Postgres-Dump**, **Git-Vault-Bundle**
und ein dokumentierter, geübter **Restore-Drill** mit festgelegtem RTO/RPO.

Rahmenbedingungen (unveränderlich):
- Host: Raspberry Pi 4 / 8 GB, 24/7 (siehe `docs/adr/0009-deployment-pi.md`).
- **SSD-Pfade:** Daten (Postgres-Datenverzeichnis, Git-Vault, Swapfile) und Backups liegen auf
  der angeschlossenen USB-3.0-SSD, **nie** auf der microSD. Backup-Ziel:
  `/mnt/ssd/stratum/backups/`.
- `stratum-db` (`pgvector/pgvector:0.8.6-pg18`), DB/Rolle `stratum`, App-Rolle `stratum_app`.
- Vault = Compose-Volume `kb`, im Container unter `/var/stratum/kb` (`Settings.kb_path`).

## 1. Zielwerte

| Kennzahl | Zielwert | Begründung / Umsetzung |
|---|---|---|
| **RPO** (max. Datenverlust) | **≤ 24 h** | Täglicher Cron-Lauf (`backup.sh`). |
| **RTO** (max. Wiederherstellzeit) | **≤ 1 h** | Custom-Dump + `--single-transaction`-Restore; Drill misst die Zeit. |
| Aufbewahrung | 14 Archive je Familie | `--keep`/`-Keep`, Default 14. |
| Restore-Test | **alle 30 Tage** | Restore-Drill (§6) in Wegwerf-DB; Ergebnis im Betriebslog. |

Der RPO von ≤ 24 h ist die *garantierte Obergrenze*. Bei Bedarf lässt sich der RPO durch einen
zweiten Cron-Lauf (z. B. tagsüber) senken — der Mechanismus ist identisch, nur häufiger.

## 2. Artefakte

Jeder Lauf schreibt **zwei** Archive mit identischem UTC-Zeitstempel:

| Datei | Inhalt | Erzeugt mit |
|---|---|---|
| `stratum-YYYYmmdd-HHMMSS.dump` | vollständige DB `stratum` (Schemas, Daten, Indizes) | `pg_dump --format=custom --no-owner --no-privileges` |
| `vault-YYYYmmdd-HHMMSS.tar.gz` | Git-Vault (`kb`), inkl. `.git` | `tar -C /var/stratum/kb -czf - .` |
| `backup.log` | Lauf-Protokoll (append) | Skript |

Ein `*.partial`-Zwischenstand wird erst nach erfolgreichem Schreiben atomar auf den Endnamen
verschoben — ein abgebrochener Lauf hinterlässt **nie** ein halbfertiges Archiv unter dem
kanonischen Namen.

## 3. Skripte

| Datei | Plattform | Rolle |
|---|---|---|
| `scripts/backup.sh` | Pi / Linux | Backup via `docker exec` (oder lokalem `pg_dump` über `--db-url`). |
| `scripts/backup.ps1` | Windows-Entwicklung | Dasselbe Verhalten über `docker compose exec`. |
| `scripts/restore.sh` | Pi / Linux | Restore / Verify / Vault-Extraktion. |
| `scripts/restore.ps1` | Windows-Entwicklung | Dasselbe über `docker compose exec`. |
| `scripts/backup_lib.py` | plattformneutral | Normative Retention-Regel (`select_backups_to_prune`). |

**Exit-Codes `backup.sh`:** 0 ok · 1 Nutzung/Config · 2 DB-Dump fehlgeschlagen ·
3 Vault-Archiv fehlgeschlagen · 4 Retention fehlgeschlagen · 5 Abhängigkeit fehlt.

**Exit-Codes `restore.sh`:** 0 ok · 1 Nutzung/Verifikation · 2 Dump fehlt/unlesbar ·
3 `pg_restore` fehlgeschlagen · 4 Vault-Archiv fehlt/unlesbar · 5 Vault-Extraktion
fehlgeschlagen · 6 Abhängigkeit fehlt.

### 3.1 Backup manuell

```bash
# Pi (SSD-Default /mnt/ssd/stratum/backups, keep=14)
./scripts/backup.sh

# Andere Parameter / anderer Pfad
./scripts/backup.sh --dir /mnt/ssd/stratum/backups --keep 21

# Trockenlauf (schreibt/löscht nichts)
./scripts/backup.sh --dry-run

# Ohne Docker, gegen einen erreichbaren Postgres
./scripts/backup.sh --db-url "postgresql://stratum@localhost:5432/stratum"
```

```powershell
# Windows-Entwicklung
.\scripts\backup.ps1
.\scripts\backup.ps1 -Dir D:\ssd\stratum\backups -Keep 14
.\scripts\backup.ps1 -DryRun
```

### 3.2 Restore

```bash
# Integrität des Dumps prüfen (keine Schreiboperation)
./scripts/restore.sh --dump /mnt/ssd/stratum/backups/stratum-20260101-030000.dump --verify

# In eine Zieldatenbank restaurieren (Objekte vorher verwerfen)
./scripts/restore.sh --dump /mnt/ssd/stratum/backups/stratum-20260101-030000.dump \
    --target-db stratum_restore_drill --force

# Nur den Vault auspacken
./scripts/restore.sh --vault /mnt/ssd/stratum/backups/vault-20260101-030000.tar.gz \
    --target-dir /tmp/kb-restored
```

```powershell
.\scripts\restore.ps1 -Dump .\backups\stratum-20260101-030000.dump -Verify
.\scripts\restore.ps1 -Dump .\backups\stratum-20260101-030000.dump -TargetDb stratum_restore_drill -Force
.\scripts\restore.ps1 -Vault .\backups\vault-20260101-030000.tar.gz -TargetDir .\kb-restored
```

Die Restore-Skripte sind **nicht interaktiv**: alle Eingaben kommen als Argument bzw.
Umgebungsvariable. `--verify` schreibt nichts; ein echter Restore verlangt `--target-db`.

**Datenbank-Nummerierung:** `restore.sh` und `restore.ps1` erzeugen die Zieldatenbank, falls sie
fehlt (`createdb` / Compose `psql`), weil `pg_restore` eine nicht existierende DB nicht anlegen
kann. Mit `--force` (`-Force`) steht `--clean --if-exists` voran, sodass der Restore idempotent
wiederholbar ist.

## 4. Retention

Pro Familie werden die **neuesten N** Archive behalten (Default 14), der Rest gelöscht.
Die Regel ist rein namensbasiert: Der Zeitstempel `YYYYmmdd-HHMMSS` ist festbreit und damit
lexikografisch chronologisch. Die normative Referenz ist
`scripts/backup_lib.py::select_backups_to_prune(names, keep)`; `backup.sh` spiegelt sie mit
`sort -r`, `backup.ps1` mit `Sort-Object Name -Descending`.

**Sicherheitsregel:** Dateien ohne gültigen Zeitstempel werden **nie** gelöscht — fremde Dateien
im Backup-Verzeichnis (README, Logs, manuelle Exporte) bleiben unangetastet. Pro Familie getrennt
gezählt, Dumps und Vault-Archive also unabhängig.

## 5. Zeitsteuerung (Cron / systemd-timer)

### 5.1 Cron (Pi, 03:00 UTC täglich)

```cron
# /etc/cron.d/stratum-backup
# m h dom mon dow user  command
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
STRATUM_BACKUP_DIR=/mnt/ssd/stratum/backups
STRATUM_BACKUP_KEEP=14
0 3 * * * root /opt/stratum/scripts/backup.sh >> /var/log/stratum/backup-cron.log 2>&1
```

Alternativ als crontab des Betriebs-Users (`crontab -e`):

```cron
0 3 * * * cd /opt/stratum && ./scripts/backup.sh >> /mnt/ssd/stratum/backups/cron.log 2>&1
```

### 5.2 systemd-Timer (robuster, mit Persistent=true)

```ini
# /etc/systemd/system/stratum-backup.service
[Unit]
Description=Stratum-Memory backup
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/stratum
Environment=STRATUM_BACKUP_DIR=/mnt/ssd/stratum/backups
Environment=STRATUM_BACKUP_KEEP=14
ExecStart=/opt/stratum/scripts/backup.sh
```

```ini
# /etc/systemd/system/stratum-backup.timer
[Unit]
Description=Daily Stratum-Memory backup

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

Aktivieren: `systemctl enable --now stratum-backup.timer`.
Verpasste Läufe werden durch `Persistent=true` nachgeholt (schützt den RPO, wenn der Pi
zur geplanten Zeit aus war).

### 5.3 Windows (geplante Aufgabe)

```powershell
$action  = New-ScheduledTaskAction -Execute 'pwsh' `
    -Argument '-NoProfile -File C:\path\to\Stratum-Memory\scripts\backup.ps1'
$trigger = New-ScheduledTaskTrigger -Daily -At 03:00
Register-ScheduledTask -TaskName 'Stratum-Backup' -Action $action -Trigger $trigger -RunLevel Highest
```

## 6. Restore-Drill (Wegwerf-Umgebung) — alle 30 Tage

Ziel: beweisen, dass ein beliebiges Archiv **vollständig** wiederherstellbar ist, und das RTO
messen. **Niemals** in die Produktions-DB restaurieren.

```bash
set -e
BACKUP_DIR=/mnt/ssd/stratum/backups
NEWEST_DUMP=$(ls -1t "$BACKUP_DIR"/stratum-*.dump | head -n1)
NEWEST_VAULT=$(ls -1t "$BACKUP_DIR"/vault-*.tar.gz | head -n1)

# 1. Integrität (schnell)
time ./scripts/restore.sh --dump "$NEWEST_DUMP" --verify

# 2. In Wegwerf-DB restaurieren und Zeit messen  <-- das ist das RTO
time docker exec -i stratum-db createdb -U stratum stratum_restore_drill
time ./scripts/restore.sh --dump "$NEWEST_DUMP" \
    --target-db stratum_restore_drill --force

# 3. Plausibilität: Kerntabellen zählen
docker exec -i stratum-db psql -U stratum -d stratum_restore_drill -c \
  "SELECT (SELECT count(*) FROM facts) AS facts,
          (SELECT count(*) FROM entities) AS entities,
          (SELECT count(*) FROM memory_events) AS events,
          (SELECT max(created_at) FROM memory_events) AS newest_event;"

# 4. Vault auspacken und Git-Integrität prüfen
rm -rf /tmp/kb-drill && mkdir -p /tmp/kb-drill
./scripts/restore.sh --vault "$NEWEST_VAULT" --target-dir /tmp/kb-drill
git -C /tmp/kb-drill fsck --full

# 5. Aufräumen
docker exec -i stratum-db dropdb -U stratum stratum_restore_drill
rm -rf /tmp/kb-drill
```

**Abnahmekriterien des Drills**
1. `restore.sh --verify` endet mit Exit 0 (`verify: OK`).
2. Der Restore endet mit Exit 0; der gemessene `time`-Wert liegt **< 1 h** (RTO-Ziel).
3. `facts`/`entities`/`memory_events` sind > 0 und `newest_event` liegt höchstens ~1 Tag
   vor dem Backup-Zeitstempel (RPO-Beleg).
4. `git fsck --full` im entpackten Vault meldet keine Fehler.

Ergebnis (Datum, Dump-Name, gemessene Zeit, Exit-Codes) ins Betriebslog schreiben
(z. B. `docs/operations/drill-log.md`).

## 7. Offsite-Option

Die SSD-Redundanz schützt nicht gegen Diebstahl, Brand oder Blitze. Offsite ist optional, aber
empfohlen. Prinzip: **Artefakte sind selbst-tragend** — ein Dump plus ein Vault-Tarball genügen
für eine vollständige Wiederherstellung.

```bash
# Verschlüsselt auf einen Offsite-Speicher spiegeln (Beispiel: rsync über SSH, SCP/tailscale)
gpg --symmetric --cipher-algo AES256 "$BACKUP_DIR/stratum-$STAMP.dump"
gpg --symmetric --cipher-algo AES256 "$BACKUP_DIR/vault-$STAMP.tar.gz"
rsync -av --remove-source-files ./*.gpg offsite@host:/vol/stratum/
```

Alternativen: `restic`/`borg` (deduplizierend, selbst verschlüsselnd) direkt gegen
`$BACKUP_DIR`, oder ein S3-kompatibles Bucket via `rclone`. In jedem Fall gilt: Offsite-Kopien
**verschlüsselt**, weil der Dump personenbezogene Inhalte und Vault-Dateien enthält.

## 8. Migrationen

Schema-Änderungen laufen versioniert als SQL unter `init/` (001…004) bzw. via Alembic
(MASTER-PLAN §15). Der Ablauf bindet das Backup ein:

1. **Vor jeder Migration** ein Backup fahren: `./scripts/backup.sh`.
2. Migration anwenden (Alembic `upgrade` / neues `init/`-Skript).
3. Bei Fehlschlag: `alembic downgrade -1`; hilft das nicht, Restore aus Schritt 1 in eine
   Wegwerf-DB prüfen und erst dann produktiv einspielen.

## 9. Troubleshooting

| Symptom | Ursache | Abhilfe |
|---|---|---|
| Exit 5 `docker not found` | Docker/Compose fehlt | Docker daemon starten; `STRATUM_DOCKER` auf den `docker`-Pfad setzen. |
| Exit 2 `dump is empty` | Container nicht healthy | `docker compose ps`; `depends_on: service_healthy` abwarten. |
| Exit 3 `target database does not exist` | `createdb` fehlgeschlagen | Rechte der Rolle prüfen; DB manuell anlegen. |
| Retention löscht nichts | Dateinamen ohne Zeitstempel | Nur `stratum-*.dump` / `vault-*.tar.gz` werden verwaltet. |
| Backup-Lauf verpasst | Pi aus | `Persistent=true` (systemd) oder `@reboot`-Cron ergänzen. |
