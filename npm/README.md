# stratum-memory (npm)

**This npm package is a thin wrapper around the Python CLI.** It contains no
application code of its own: `bin/stratum.js` only locates the real `stratum`
command line tool and forwards your arguments to it. The Python package of the
same name (`stratum-memory`, CLI `stratum`) is what actually implements
everything; the server stack (`stratum install`, `stratum up`, …) additionally
requires **Docker**.

Because of that, the wrapper **never installs anything by itself** — no
`postinstall` script, no hidden download. If no Python CLI is found it prints
installation instructions and exits with code `3`.

## Install

```console
npm install -g stratum-memory     # global: provides the `stratum` command
npx stratum-memory version        # one-off, no global install
```

After installing the npm wrapper you still need the Python CLI (once):

### Windows

```powershell
pipx install stratum-memory                     # recommended
py -3 -m pip install --user stratum-memory      # without pipx
winget install TotalBassJunkie.StratumMemory    # or: Scoop, MSI / EXE installer
irm https://get.stratum.dev/install.ps1 | iex   # one-line PowerShell installer
```

Python 3.12+ is required (`winget install Python.Python.3.12`).

### macOS

```bash
pipx install stratum-memory                     # recommended
python3 -m pip install --user stratum-memory    # without pipx
brew install TotalBassJunkie/stratum/stratum-memory
curl -fsSL https://get.stratum.dev/install.sh | bash
```

### Linux

```bash
pipx install stratum-memory                     # recommended
python3 -m pip install --user stratum-memory    # without pipx
curl -fsSL https://get.stratum.dev/install.sh | bash
# or .deb / .rpm / AppImage / Snap / Homebrew on Linux
```

## Usage

The wrapper is transparent — every argument, exit code, stream and signal goes
to the Python CLI:

```console
stratum version      # print the version
stratum doctor       # environment diagnostics (python, docker, ports)
stratum install      # bootstrap the local stack (compose + .env, pull image)
stratum up           # start the stack (docker compose up -d)
stratum down         # stop the stack
stratum status       # container + health summary
stratum logs [svc]   # tail logs
stratum mcp          # run the MCP server (stdio / streamable-http)
stratum health       # readiness probe (JSON)
```

`install`, `up`, `down`, `status` and `logs` shell out to `docker compose` and
therefore need **Docker** (Docker Desktop on Windows/macOS). `mcp` and `health`
work against any configured `STRATUM_DB_*` (they default to the stack).

## How the shim finds the CLI

1. a `stratum` executable on `PATH` — **excluding this wrapper itself** (calling
   npm's own bin wrapper again would recurse forever);
2. otherwise `python3 -m stratum.cli`, then `python -m stratum.cli`, then
   `py -3 -m stratum.cli` (each only if it can `import stratum.cli` on
   Python >= 3.12);
3. otherwise a clear error with the install hints above and **exit code 3**.

## Requirements

| | |
|---|---|
| Node.js | >= 18 (only to run the wrapper) |
| Python | >= 3.12 (the actual CLI) |
| Docker | only for the server stack (`install` / `up` / `down` / `status` / `logs`) |

## Links

- Repository: <https://github.com/TotalBassJunkie/stratum-memory>
- Issues: <https://github.com/TotalBassJunkie/stratum-memory/issues>
- License: Apache-2.0
