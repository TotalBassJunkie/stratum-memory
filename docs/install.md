# Stratum-Memory — Installation

Diese Anleitung führt durch **jeden** Installationsweg für das `stratum`-CLI und
den Server-Stack. Der verbindliche Vertrag über alle Kanäle (Paketnamen,
Artefaktnamen, Release-Assets) steht in **`docs/distribution.md`**; hier steht nur,
wie ein Mensch installiert.

Alle Kanäle installieren dasselbe: das Kommandozeilenwerkzeug **`stratum`**. Der
Server-Stack selbst (Postgres 18 + pgvector, `stratum-core`, `stratum-worker`)
wird davon **nicht** mitinstalliert — den zieht `stratum install` als Container.

## Kurzfassung (die drei schnellsten Wege)

```bash
# Linux / macOS
curl -fsSL https://get.stratum.dev/install.sh | bash
```

```powershell
# Windows (PowerShell)
irm https://get.stratum.dev/install.ps1 | iex
```

```bash
# Überall, wo Python >= 3.12 vorhanden ist
pipx install stratum-memory
```

Danach in **jedem** Fall derselbe Ablauf: `stratum install` → `stratum up` →
`stratum status` (siehe [Nach der Installation](#nach-der-installation)).

## Voraussetzungen

| Was | Wofür | Details |
|---|---|---|
| **Python ≥ 3.12** | für **alle** Kanäle **außer** MSI, EXE und Docker | Nur die PyInstaller-Installer (MSI/EXE) und das Container-Image bringen ihre eigene Laufzeit mit. Jeder andere Kanal startet den **System-Interpreter** — auch `.deb`/`.rpm`, Snap, AppImage und `.pkg`/`.dmg`. Die Zuordnung Kanal für Kanal steht in der Tabelle direkt darunter. |
| **Docker** (Engine bzw. Docker Desktop) | für den Server-Stack (`stratum install`, `up`, `down`, `status`, `logs`) | Diese Unterbefehle rufen `docker compose` auf. Ohne Docker läuft nur das CLI (z. B. `stratum version`, `stratum doctor`, `stratum mcp` gegen eine **bereits** erreichbare Datenbank). |
| **PostgreSQL 18 + pgvector** | für die Datenhaltung | **Nicht** selbst installieren: `stratum install` schreibt eine `docker-compose.yml`, die Postgres mitbringt. Nur wer den MCP-Server gegen eine fremde DB betreibt, braucht sie separat (`STRATUM_DB_*`). |
| **Netzwerk** | Download der Release-Artefakte bzw. des Container-Images | `ghcr.io` (Image) und `github.com`/`get.stratum.dev` (Assets, Skripte). |
| **Kein `sudo`/Admin nötig** | `pipx`, `npm` und der `curl`-Einzeiler unter Linux/macOS | Diese Wege installieren in das Benutzerverzeichnis (`~/.local/bin` bzw. den npm-Prefix). Nur `.deb`/`.rpm`/Snap/`.pkg`/MSI/EXE brauchen erhöhte Rechte — das `.pkg` läuft als root (`sudo installer -pkg …`), weil sein `postinstall` den Symlink `/usr/local/bin/stratum` anlegt. |

### Braucht der Kanal ein eigenes Python ≥ 3.12?

Die native Verpackung ist **kein** Python-Freeze: `.deb`/`.rpm`, Snap, AppImage,
`.pkg`/`.dmg`, Homebrew und die beiden Skript-Installer legen nur das
`stratum`-Kommando ab und starten es mit dem **Interpreter des Hosts**. Nur MSI
und EXE frieren den Interpreter mit PyInstaller ein.

| Kanal | Braucht Python ≥ 3.12? | Warum |
|---|:---:|---|
| `.deb` / `.rpm` | **ja** | Das Paket deklariert `depends: python3 (>= 3.12)` (`packaging/linux/nfpm.yaml`); `/usr/bin/stratum` ist ein POSIX-sh-Launcher, der `python3 -m stratum.cli` startet. |
| Snap | **ja** | `stage-packages: python3` — den Interpreter bringt die Snap-Base mit, nicht das CLI-Payload; gestartet wird er über denselben Launcher wie im `.deb`. |
| AppImage | **ja** | Das Image bündelt nur das Wheel; `AppRun` ruft den Launcher auf, der `python3` vom Host sucht. |
| `.pkg` / `.dmg` | **ja** | `postinstall` und der macOS-Launcher nutzen das System-`python3` („Python 3.12 or newer" steht in den Installer-Hinweisen). |
| Homebrew (`brew`) | **ja** | Die Formel hängt an `python@3.12` (`depends_on "python@3.12"`). |
| curl / `install.sh` | **ja** | Das Skript sucht `python3`/`python` ≥ 3.12 und bricht sonst mit Exit-Code 1 ab. |
| PowerShell / `install.ps1` | **ja** | Ebenso: ohne Python ≥ 3.12 endet das Skript mit Exit-Code 1. |
| pipx / pip | **ja** | `pipx install stratum-memory` baut die venv auf dem System-Python auf. |
| npm | **ja** | Der Wrapper startet das CLI über das System-Python. |
| MSI | **nein** | PyInstaller-Freeze: `stratum.exe` enthält Interpreter und Stdlib, das MSI bringt eine eigene Laufzeit mit. |
| EXE (Inno Setup) | **nein** | Derselbe PyInstaller-Freeze wie im MSI — der Setup.exe-Installer bringt eine eigene Laufzeit mit. |
| Docker | **nein** | Das Image `ghcr.io/totalbassjunkie/stratum-memory` enthält Python und das Paket. |

**Konkretes Symptom (z. B. Ubuntu 22.04, System-Python 3.10):**

```bash
# .deb: apt lehnt die Installation wegen der deklarierten Abhängigkeit ab.
sudo apt install ./stratum_<version>_amd64.deb
# → stratum-memory : Depends: python3 (>= 3.12) but 3.10.x is to be installed

# AppImage: der Launcher findet zwar python3, aber das Paket ist dort nicht
# installiert — der Start endet im Import-Fehler.
./Stratum-<version>-x86_64.AppImage version
# → ModuleNotFoundError: No module named 'stratum'
```

Auf einem solchen Host zuerst ein Python ≥ 3.12 bereitstellen (`brew install
python@3.12` unter macOS; unter Ubuntu 22.04 etwa der `deadsnakes`-PPA oder ein
Upgrade auf 24.04) — oder auf einen Kanal ausweichen, der keinen
System-Interpreter braucht: **MSI/EXE** (Windows) bzw. **Docker**.

Prüfen, ob alles da ist:

```bash
stratum doctor     # prüft Python, Docker, Ports und die Konfiguration
```

## Nach der Installation

Jeder Kanal installiert nur das CLI. Der Stack kommt danach in drei Schritten —
**Docker muss laufen** (`docker info` muss antworten), sonst bricht `stratum up`
mit Exit-Code 2 ab:

```bash
# 1. Stack-Verzeichnis anlegen: docker-compose.yml + .env + secrets/ schreiben,
#    Container-Image ziehen. Ohne Docker schlägt dieser Schritt fehl.
stratum install

# 2. Stack starten (im Hintergrund) — ruft intern `docker compose up -d` auf.
stratum up

# 3. Zustand ansehen: Container + Health-Übersicht.
stratum status
```

Nützliche Nachbarn:

```bash
stratum logs [service]   # Logs mitlesen
stratum down             # Stack stoppen
stratum version          # installierte Version
stratum doctor           # Umgebungsdiagnose
stratum health           # Readiness-Probe (JSON)
stratum mcp              # MCP-Server (stdio / streamable-http)
```

## Windows

### 1. winget (empfohlen, sobald das Paket im Community-Repo liegt)

```powershell
winget install TotalBassJunkie.StratumMemory
```

> **Ehrlicher Hinweis:** Das Manifest liegt im Repository
> (`packaging/winget/`), ist aber **noch nicht** in `microsoft/winget-pkgs`
> eingereicht. Solange das Paket nicht im Community-Repo ist, schlägt der Befehl
> oben fehl („No package found matching input criteria"). Der Erstupload ist ein
> einmaliger, **nicht automatisierbarer** Schritt (`wingetcreate new` öffnet den
> PR von Hand); danach findet winget das Paket. Bis dahin diesen Kanal
> überspringen und **MSI/EXE** (Abschnitt 6), **Scoop** (2), **npm** (3) oder den
> **PowerShell-Einzeiler** (5) nutzen.

### 2. Scoop

```powershell
scoop bucket add stratum https://github.com/TotalBassJunkie/scoop-stratum
scoop install stratum-memory
```

### 3. npm

```powershell
npm install -g stratum-memory
```

### 4. Chocolatey

```powershell
choco install stratum-memory
```

### 5. PowerShell-Einzeiler

```powershell
irm https://get.stratum.dev/install.ps1 | iex
```

Dieselbe Datei direkt von GitHub (funktioniert immer, siehe
[`get.stratum.dev`](#hinweis-zu-getstratumdev)):

```powershell
irm https://raw.githubusercontent.com/TotalBassJunkie/stratum-memory/main/install.ps1 | iex
```

### 6. MSI- oder EXE-Installer aus dem GitHub-Release

Beide Dateien liegen am Release `v<version>`:

```powershell
# MSI (still, ohne Rückfragen)
msiexec /i Stratum-<version>-x64.msi /qn

# EXE (Inno Setup) — interaktiv per Doppelklick, oder still:
Stratum-<version>-setup.exe /VERYSILENT
```

Release-Seite im Browser: `https://github.com/TotalBassJunkie/stratum-memory/releases/latest`
— dort `Stratum-<version>-x64.msi` bzw. `Stratum-<version>-setup.exe` herunterladen.

### 7. pipx / pip

```powershell
pipx install stratum-memory
# oder, ohne pipx:
python -m pip install --user stratum-memory
```

### 8. Docker Desktop

```powershell
docker run --rm ghcr.io/totalbassjunkie/stratum-memory version
```

Für den vollen Stack statt `docker run` einfach `stratum install` + `stratum up`
verwenden — beide nutzen dasselbe Image.

## Linux

### 1. curl-Einzeiler (primärer Weg, ohne Paketmanager)

```bash
curl -fsSL https://get.stratum.dev/install.sh | bash
```

Dieselbe Datei direkt von GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/TotalBassJunkie/stratum-memory/main/install.sh | bash
```

### 2. .deb (Debian / Ubuntu)

```bash
curl -LO https://github.com/TotalBassJunkie/stratum-memory/releases/latest/download/stratum_<version>_amd64.deb
sudo apt install ./stratum_<version>_amd64.deb
```

### 3. .rpm (Fedora / RHEL / openSUSE)

```bash
curl -LO https://github.com/TotalBassJunkie/stratum-memory/releases/latest/download/stratum-<version>-1.x86_64.rpm
sudo dnf install ./stratum-<version>-1.x86_64.rpm
```

### 4. AppImage (ohne Installation)

```bash
curl -LO https://github.com/TotalBassJunkie/stratum-memory/releases/latest/download/Stratum-<version>-x86_64.AppImage
chmod +x Stratum-<version>-x86_64.AppImage
./Stratum-<version>-x86_64.AppImage version
```

### 5. Snap

```bash
sudo snap install stratum-memory
```

### 6. Homebrew on Linux

```bash
brew install TotalBassJunkie/stratum/stratum-memory
```

### 7. pipx / pip

```bash
pipx install stratum-memory
# oder, ohne pipx:
python -m pip install --user stratum-memory
```

### 8. Docker

```bash
docker run --rm ghcr.io/totalbassjunkie/stratum-memory version
```

## macOS (Apple)

### 1. Homebrew (empfohlen)

```bash
brew install TotalBassJunkie/stratum/stratum-memory
```

### 2. curl-Einzeiler

```bash
curl -fsSL https://get.stratum.dev/install.sh | bash
```

### 3. .pkg-Installer (Doppelklick)

```bash
curl -LO https://github.com/TotalBassJunkie/stratum-memory/releases/latest/download/Stratum-<version>-x86_64.pkg
open Stratum-<version>-x86_64.pkg      # öffnet den Installer-Assistenten
```

Im Finder genügt ein Doppelklick auf `Stratum-<version>-x86_64.pkg` und dann
„Weiter" bis „Installieren". Alternativ still per Terminal:

```bash
sudo installer -pkg Stratum-<version>-x86_64.pkg -target /
```

Der `.pkg`-Installer läuft als **root** (`rootVolumeOnly="true"`): sein
`postinstall`-Skript legt den Symlink `/usr/local/bin/stratum` auf den
Launcher `/usr/local/lib/stratum/bin/stratum` an. Er braucht deshalb — anders
als pipx, npm und der `curl`-Einzeiler — erhöhte Rechte und ein
**System-Python ≥ 3.12**; das `.pkg` bringt keinen eigenen Interpreter mit.

### 4. .dmg (Drag & Drop in „Programme")

```bash
curl -LO https://github.com/TotalBassJunkie/stratum-memory/releases/latest/download/Stratum-<version>-x86_64.dmg
open Stratum-<version>-x86_64.dmg      # Image öffnen
```

Im geöffneten Fenster das Symbol **Stratum** auf den Ordner **Programme** ziehen,
danach das Image auswerfen. Der Einzeiler in `Programme/Stratum` ist danach
aufrufbar. Das `.dmg` enthält **dasselbe `.pkg`** und ruft damit denselben
root-Installer auf — es braucht also ebenfalls erhöhte Rechte und ein
**System-Python ≥ 3.12**.

### 5. pipx / pip

```bash
pipx install stratum-memory
# oder, ohne pipx:
python -m pip install --user stratum-memory
```

### 6. npm

```bash
npm install -g stratum-memory
```

### 7. Docker

```bash
docker run --rm ghcr.io/totalbassjunkie/stratum-memory version
```

## Kanal × Betriebssystem

Ein Häkchen heißt: auf diesem Betriebssystem unterstützt und oben dokumentiert.

| Kanal | Windows | Linux | macOS |
|---|:---:|:---:|:---:|
| winget (`TotalBassJunkie.StratumMemory`) | ✔ | – | – |
| Scoop (`stratum-memory`) | ✔ | – | – |
| npm (`stratum-memory`) | ✔ | – | ✔ |
| Chocolatey (`stratum-memory`) | ✔ | – | – |
| curl / `install.sh` | – | ✔ | ✔ |
| PowerShell / `install.ps1` | ✔ | – | – |
| MSI-Installer | ✔ | – | – |
| EXE-Installer | ✔ | – | – |
| .deb | – | ✔ | – |
| .rpm | – | ✔ | – |
| AppImage | – | ✔ | – |
| Snap (`stratum-memory`) | – | ✔ | – |
| Homebrew (`brew`) | – | ✔ | ✔ |
| .pkg-Installer | – | – | ✔ |
| .dmg-Image | – | – | ✔ |
| pipx / pip (`stratum-memory`) | ✔ | ✔ | ✔ |
| Docker / Docker Desktop (`ghcr.io/totalbassjunkie/stratum-memory`) | ✔ | ✔ | ✔ |

Abgedeckte Kanäle je Betriebssystem: **Windows 9**, **Linux 8**, **macOS 7**.

## Hinweis zu `get.stratum.dev`

`get.stratum.dev` ist **kein eigener Server**, sondern nur ein **CNAME** auf die
GitHub-Raw-Dateien. Die kurzen URLs sind reine Bequemlichkeit:

| Kurz | Lang (funktioniert immer) |
|---|---|
| `https://get.stratum.dev/install.sh` | `https://raw.githubusercontent.com/TotalBassJunkie/stratum-memory/main/install.sh` |
| `https://get.stratum.dev/install.ps1` | `https://raw.githubusercontent.com/TotalBassJunkie/stratum-memory/main/install.ps1` |

Ist der CNAME (noch) nicht eingerichtet oder blockt ihn ein Firmen-Proxy, immer
die `raw.githubusercontent.com`-URL verwenden — sie ist der Ursprung, auf den der
CNAME zeigt.

## Deinstallation

| Kanal | Befehl |
|---|---|
| winget | `winget uninstall TotalBassJunkie.StratumMemory` (nur wenn der Kanal verfügbar war — siehe [Windows → 1. winget](#1-winget-empfohlen-sobald-das-paket-im-community-repo-liegt)) |
| Scoop | `scoop uninstall stratum-memory` |
| npm | `npm uninstall -g stratum-memory` |
| Chocolatey | `choco uninstall stratum-memory` |
| PowerShell-Einzeiler | `stratum` aus dem Benutzer-`PATH` entfernen (das Skript legt eine `stratum.exe`/`stratum.ps1` im Benutzerverzeichnis ab) und den Ordner löschen |
| MSI | `msiexec /x Stratum-<version>-x64.msi /qn` — oder „Apps & Features" → Stratum-Memory → Deinstallieren |
| EXE | „Apps & Features" → Stratum-Memory → Deinstallieren. Oder still per Kommandozeile: `"%LOCALAPPDATA%\Programs\Stratum\unins000.exe" /VERYSILENT` — das Uninstall-Programm (`unins000.exe`) liegt im Installationsverzeichnis, standardmäßig `%LOCALAPPDATA%\Programs\Stratum` (bei einer Installation mit `/ALLUSERS`: `%ProgramFiles%\Stratum`). |
| .deb | `sudo apt remove stratum-memory` (bzw. `sudo dpkg -r stratum-memory`) |
| .rpm | `sudo dnf remove stratum-memory` (bzw. `sudo rpm -e stratum-memory`) |
| AppImage | Datei `Stratum-<version>-x86_64.AppImage` löschen |
| Snap | `sudo snap remove stratum-memory` |
| Homebrew | `brew uninstall stratum-memory` |
| .pkg | `sudo rm -rf /usr/local/bin/stratum` (der Symlink) **und** `sudo rm -rf /usr/local/lib/stratum` (die Nutzlast), bzw. den Empfangs-Eintrag unter `/var/db/receipts/` entfernen |
| .dmg | `Stratum` aus `Programme` in den Papierkorb ziehen |
| pipx | `pipx uninstall stratum-memory` |
| pip | `python -m pip uninstall stratum-memory` |
| Docker | `docker rmi ghcr.io/totalbassjunkie/stratum-memory` |

Der **Server-Stack** ist von der CLI-Deinstallation getrennt:

```bash
stratum down                                  # Container stoppen
docker compose -f <install-dir>/docker-compose.yml down -v   # inkl. Volumes/Daten
```

`-v` löscht die Postgres-Volumes und damit **alle** Daten — vorher
`docs/backup-restore.md` lesen. Das Installationsverzeichnis (Compose-Datei,
`.env`, `secrets/`) wird nicht automatisch entfernt.

## Verwandte Dokumente

- `docs/distribution.md` — Vertrag: Paketnamen, Artefaktnamen, Release-Assets, Kanal-Matrix
- `docs/operations-handbuch.md` — Betrieb, Health, Logs, Rollback, Troubleshooting
- `docs/backup-restore.md` — Backup/Restore, Retention, RTO/RPO
- `docs/dashboard.md` — Operator-Dashboard
- `docs/api-reference.md` — die zehn MCP-Tools
