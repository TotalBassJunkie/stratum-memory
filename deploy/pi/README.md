# Stratum-Memory — Raspberry Pi 4 / 8 GB Deployment (WP-14)

Betriebsprofil für den Dauerbetrieb (24/7) auf einem **Raspberry Pi 4 mit 8 GB RAM**.
Unveränderliche Rahmenbedingungen: `MASTER-PLAN.md` §10 und
`docs/adr/0009-deployment-pi.md`.

Kernpunkte:
- **Drei Container**: `stratum-db` (≤ 2 GB), `stratum-core` (≤ 1 GB), `stratum-worker` (≤ 1.5 GB).
- **DB ist swapfrei**: `memswap_limit == mem_limit` (2g). Das ist die realistischste Umsetzung
  der zram-Vorgabe bei gleichzeitiger DB-Stabilität (kein Postgres-Thrashing).
- **Swap**: 1 GB **zram** (Prio 100, nur System-Pages) + 4 GB **SSD-Swapfile** (Prio 10).
  **Kein microSD-Swap.**
- **Daten auf SSD**: `pgdata`, Git-Vault (`kb`) und Backups liegen unter `/mnt/ssd/stratum/`.
- **`vm.swappiness=10`, `vm.vfs_cache_pressure=50`, `vm.overcommit_memory=0`.**

---

## 1. Voraussetzungen

| Komponente | Vorgabe |
|---|---|
| Hardware | Raspberry Pi 4, **8 GB**, USB-3.0-SSD dauerhaft angeschlossen |
| OS | Raspberry Pi OS (64-bit) / Debian, `systemd` |
| Bootmedium | microSD **nur zum Booten** (kein Datenspeicher, kein Swap) |
| SSD-Mount | `/mnt/ssd` (per `/etc/fstab`, `nofail`) |
| Docker | Docker Engine + Compose-Plugin (`docker compose`) |

> **Warum SSD?** Postgres-Datenverzeichnis und Git-Vault verursachen viele Schreibvorgänge.
> Auf der microSD führt das zu Verschleiß und hoher Latenz; zudem ist der Swap auf der
> microSD bei 24/7-Betrieb ein Ausfallrisiko (ADR-0009).

---

## 2. SSD mounten (einmalig)

Die SSD per USB 3.0 anschließen, dann (Beispiel für `/dev/sda1`, Pfad prüfen!):

```bash
sudo blkid                     # Partition der SSD finden (UUID notieren)
sudo mkdir -p /mnt/ssd
```

Eintrag in `/etc/fstab` (UUID statt `/dev/sda1` verwenden; `nofail` verhindert Boothänger
ohne SSD):

```
UUID=<deine-ssd-uuid>  /mnt/ssd  ext4  defaults,noatime,nofail  0  2
```

```bash
sudo mount -a
findmnt /mnt/ssd               # muss den Mount zeigen
```

Dann die Datenverzeichnisse anlegen und die Host-Konfiguration einspielen — das erledigt
das Provisionierungsskript:

```bash
cd /pfad/zu/Stratum-Memory
sudo bash deploy/pi/setup-pi.sh
```

`setup-pi.sh` ist idempotent und macht:
1. Verzeichnisse `/mnt/ssd/stratum/{pgdata,kb,backups}` **mit den passenden
   Container-Ownern** (siehe unten),
2. Docker installieren, falls nicht vorhanden,
3. Swap/zram/sysctl über `deploy/pi/swap-setup.sh` setzen,
4. `secrets/`-Platzhalter mit restriktiven Rechten anlegen.

> **Ownership der Bind-Mounts ist entscheidend.** Die Container laufen **nicht** als
> root: Core und Worker als `stratum` (UID/GID **10001**, siehe `Dockerfile`), die DB
> als `postgres` (UID/GID **999** im `pgvector/pgvector`-Image). Ohne passendes
> Ownership bekommt der Container `EACCES` auf sein Bind-Mount-Verzeichnis. Das
> Skript setzt deshalb:
>
> ```bash
> install -d -m 0700 -o 999   -g 999   /mnt/ssd/stratum/pgdata   # Postgres (UID 999)
> install -d -m 0755 -o 10001 -g 10001 /mnt/ssd/stratum/kb       # Core/Worker (UID 10001)
> ```
>
> Bei bestehenden Verzeichnissen aus einer früheren Version nachziehen:
>
> ```bash
> sudo chown -R 999:999     /mnt/ssd/stratum/pgdata
> sudo chown -R 10001:10001 /mnt/ssd/stratum/kb
> ```

> Nur Swap/VM-Tuning gewünscht? Dann direkt `sudo bash deploy/pi/swap-setup.sh` ausführen.
> Das Skript findet seinen Quellordner selbst und installiert
> `/etc/sysctl.d/99-stratum.conf` und `/etc/default/zramswap`.

### 2.1 Swap & zram manuell prüfen

```bash
swapon --show
# NAME              TYPE      SIZE  USED PRIO
# /mnt/ssd/swapfile file      4G    ...    10
# /dev/zram0        partition 1G    ...   100

sysctl vm.swappiness vm.vfs_cache_pressure vm.overcommit_memory
# vm.swappiness = 10
# vm.vfs_cache_pressure = 50
# vm.overcommit_memory = 0
```

`swap-setup.sh` legt das SSD-Swapfile an (`fallocate` mit `dd`-Fallback), macht es mit
`mkswap` nutzbar, aktiviert es mit **Prio 10** und trägt es idempotent in `/etc/fstab` ein.
Zusätzlich wird der Debian-Dienst `zramswap.service` (Konfiguration
`deploy/pi/zram-config.conf` → `/etc/default/zramswap`) aktiviert: **1 GB, Prio 100, zstd**.

---

## 3. Secrets anlegen

Docker-Secrets liegen als Dateien unter `secrets/` (nicht im Git, nicht in Env-Variablen):

```bash
cd /pfad/zu/Stratum-Memory
mkdir -p secrets && chmod 700 secrets

# 1) Passwort der Owner-/Superuser-Rolle 'stratum' (nur Init/Migration/Dump)
openssl rand -base64 32 | tr -d '\n' > secrets/pg_password

# 2) Passwort der nicht-privilegierten App-Rolle 'stratum_app' (RLS-unterworfen)
openssl rand -base64 32 | tr -d '\n' > secrets/app_password

# 3) API-Key des LLM-/Embedding-Endpunkts
printf '%s' 'DEIN_LLM_API_KEY' > secrets/llm_key

chmod 600 secrets/*
```

> **`app_password`** wird erst nach dem ersten DB-Start in die Rolle `stratum_app` gesetzt
> (siehe `init/002_app_role.sql`). Die Trennung von Owner (`stratum`) und App-Rolle
> (`stratum_app`) ist sicherheitsrelevant — Core und Worker laufen **nur** unter `stratum_app`.

Optional: `LLM_BASE_URL` in eine `.env` im Repo-Wurzel legen (der Base-Compose liest
`${LLM_BASE_URL:-}`):

```bash
echo 'LLM_BASE_URL=https://dein-endpunkt/v1' >> .env
```

---

## 4. Starten

Immer **beide** Compose-Dateien angeben (Basis + Pi-Override):

```bash
cd /pfad/zu/Stratum-Memory
docker compose \
  -f docker-compose.yml \
  -f deploy/pi/docker-compose.override.yml \
  up -d
```

Der Override:
- setzt die RAM-Limits (`db 2g`, `core 1g`, `worker 1.5g`),
- hält die **DB swapfrei** (`memswap_limit == mem_limit == 2g`),
- bindet `pgdata` und `kb` an `/mnt/ssd/stratum/...` (statt an die microSD),
- setzt `restart: unless-stopped`.

Logs folgen:

```bash
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml logs -f stratum-db
```

---

## 5. Health-Check

### 5.1 Container-Status

```bash
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml ps
# stratum-db      ... (healthy)
# stratum-core    ... Up
# stratum-worker  ... Up
```

### 5.2 DB erreichbar

```bash
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml \
  exec stratum-db pg_isready -U stratum -d stratum
# /var/run/postgresql:5432 - accepting connections
```

### 5.3 MCP-Endpoint (Streamable-HTTP)

`stratum-core` bindet **im Container an `0.0.0.0:8088`**, host-seitig ist der Port über das
Port-Mapping auf **`127.0.0.1:8088`** beschränkt (kein öffentlicher Zugang):

```bash
curl -sS http://127.0.0.1:8088/health
# {"status":"ok","db":true,"git":true,"llm":true,"queued_jobs":0}
```

### 5.4 DB swapfrei verifizieren (Akzeptanzkriterium WP-14)

```bash
docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' stratum-db
# beide Werte gleich und == 2147483648 (2 GiB)
```

`Memory == MemorySwap` (2 GiB) bestätigt: der DB-Container hat **keinen** Swap
(weder zram noch SSD).

### 5.5 SSD-Bindung verifizieren

```bash
docker inspect -f '{{json .Mounts}}' stratum-core | grep -o '/mnt/ssd/stratum/kb'
docker volume ls | grep -i pgdata || echo "pgdata ist ein Bind-Mount (kein Named Volume) — erwartet"
```

---

## 6. `docker stats` — Erwartungswerte

```bash
docker stats --no-stream
```

| Container | MEM LIMIT | Erwarteter MEM USAGE (Leerlauf) | Erwarteter MEM USAGE (Betrieb) |
|---|---|---|---|
| `stratum-db` | **2 GiB** | ca. 150–300 MiB | ≤ 1.5 GiB (shared_buffers 1 GB + Arbeitsspeicher) |
| `stratum-core` | **1 GiB** | ca. 80–150 MiB | ≤ 600 MiB |
| `stratum-worker` | **1.5 GiB** | ca. 60–120 MiB | ≤ 1.0 GiB (Reflect-Batches) |
| **Summe** | 4.5 GiB | ca. 0.3–0.6 GiB | ≤ 3.1 GiB |

Rest des 8-GB-RAM geht an OS/Kernel/Docker (~1.2 GB) und Filesystem-Cache (~2.3 GB) — siehe
RAM-Budget in `MASTER-PLAN.md` §10.3.

> **Erwartung ist flach.** Wächst `MEM USAGE` eines Containers monoton über Tage, ist das
> ein Leak oder ein zu großer Bestand — dann `stratum_reflect`/Bestandsgröße prüfen
> (Pi-Zielmenge: ≤ 150 k Fakten / ≤ 50 k Chunks).

---

## 7. Troubleshooting

### 7.1 OOM-Kill (`stratum-*` wird neu gestartet, `dmesg` zeigt „Out of memory")

Symptome: `docker ps` zeigt hohen `RestartCount`, `dmesg -T | grep -i "killed process"`.

Maßnahmen (in dieser Reihenfolge):
1. **Auslastung ansehen**: `docker stats --no-stream`; welche Container sind am Limit?
2. **Bestand prüfen**: sind Fakten/Chunks/Vektoren über der Pi-Zielmenge (§16.3)?
   Bei > 200 k Vektoren: Off-Pi-Reindex statt weiterer Aufstockung.
3. **Postgres konservativer**: `shared_buffers` in `docker-compose.yml` von `1GB` auf `768MB`,
   `work_mem` von `16MB` auf `8MB` senken (nur wenn die DB-OOM-Ursache ist).
4. **CPU-Throttling** (nicht OOM): laut §10.3 zuerst `stratum-worker` `cpus` auf `1.0`, dann
   `stratum-core` auf `1.0`.
5. Container neu starten: `... up -d --force-recreate stratum-<name>`.

> Die `restart: unless-stopped`-Policy fängt einzelne OOM-Kills automatisch ab — aber sie ist
> **kein** Ersatz für die Ursachenbehebung.

### 7.2 Thrashing (Swap zu aktiv, System „hängt")

Symptome: `vmstat 1` zeigt dauerhaft `si`/`so` > 0; `docker stats` langsam; SSH träge.
Zielwert ist **`si/so ≈ 0`** (WP-14-Soak-Kriterium).

```bash
vmstat 1 5        # si/so Spalten beobachten
swapon --show     # ist das SSD-Swapfile oder zram stark belegt?
```

Ursachen & Fixes:
- **zram/SSD-Swap fälschlich für die DB aktiv** → prüfen, dass `memswap_limit == mem_limit`
  in `deploy/pi/docker-compose.override.yml` (Abschnitt 5.4); bei Abweichung Compose neu
  anwenden.
- **`vm.swappiness` zu hoch** → muss `10` sein (Abschnitt 2.1); `swap-setup.sh` erneut laufen
  lassen.
- **SSD-Swapfile fehlt/zu klein** → `swap-setup.sh` prüft und legt es neu an.
- **RAM dauerhaft am Limit** → wie bei OOM (7.1) Bestand/Limits prüfen.

### 7.3 Container kommt nicht hoch

```bash
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml \
  logs --tail=100 stratum-db
```
- `permission denied` auf `/var/lib/postgresql` → Ownership auf der SSD prüfen; der
  Postgres-Container läuft als `postgres` (UID 999 im Image). `setup-pi.sh` legt `pgdata`
  bereits mit `0700` und Owner `999:999` an; ggf. nachziehen:
  `sudo chown -R 999:999 /mnt/ssd/stratum/pgdata`.
- `permission denied` auf `/var/stratum/kb` (Core/Worker) → der Vault muss dem
  Container-User `stratum` (UID/GID 10001) gehören:
  `sudo chown -R 10001:10001 /mnt/ssd/stratum/kb`.
- „password file not found“ → `secrets/*` fehlen oder sind leer (Abschnitt 3).
- `stratum-core` „connection refused“ zur DB → `depends_on: service_healthy` wartet auf
  `pg_isready`; DB-Logs prüfen.

### 7.4 Nach Reboot ist der Swap weg

`swap-setup.sh` schreibt den `fstab`-Eintrag und aktiviert `zramswap.service`. Prüfen:
```bash
grep swapfile /etc/fstab
systemctl status zramswap.service
```
Optional als systemd-Bootstrap vor Docker: `deploy/pi/stratum-host-bootstrap.service`
(siehe Kommentare in der Unit) nach `/etc/systemd/system/` kopieren und
`systemctl enable --now`. Die Unit verlangt `RequiresMountsFor=/mnt/ssd`, damit der
Swap und die Daten-Mounts erst nach der SSD verfügbar sind.

---

## 8. Artefakt-Übersicht

| Datei | Zweck |
|---|---|
| `docker-compose.override.yml` | Pi-Limits, DB-Swap-Ausschluss, SSD-Bind-Mounts, Restart-Policy |
| `setup-pi.sh` | Einmalige Provisionierung (SSD-Layout, Docker, Swap, Secrets) |
| `swap-setup.sh` | 4 GB SSD-Swapfile (Prio 10) + 1 GB zram (Prio 100) + sysctl, idempotent |
| `zram-config.conf` | zram-Parameter → `/etc/default/zramswap` |
| `sysctl-stratum.conf` | Kernel-VM-Werte → `/etc/sysctl.d/99-stratum.conf` |
| `stratum-host-bootstrap.service` | optionale systemd-Unit: Host-Bootstrap (Swap/sysctl) vor Docker |
| `tests/test_deploy_pi.py` | DB-freie Validierung aller obigen Artefakte |

Prüfen (DB-frei, ohne Pi):

```bash
.\.venv\Scripts\python.exe -m pytest tests\test_deploy_pi.py -q -o addopts=""
```
