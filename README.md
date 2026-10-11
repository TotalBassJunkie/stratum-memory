<p align="center">
  <img src="assets/logo/stratum-logo.png" alt="Stratum-Memory" width="620">
</p>

<h1 align="center">Stratum-Memory</h1>

<p align="center">
  <em>A four-layer AI memory &amp; context system — filesystem, bi-temporal graph, vector/KV, reflect.</em>
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.12+-blue">
  <img alt="postgres" src="https://img.shields.io/badge/postgres-18%20%2B%20pgvector-336791">
  <img alt="tests" src="https://img.shields.io/badge/tests-619%20passing-brightgreen">
  <img alt="mcp" src="https://img.shields.io/badge/MCP-compatible-8A2BE2">
  <img alt="runs on" src="https://img.shields.io/badge/runs%20on-Raspberry%20Pi%204%20%7C%20server-C51A4A">
</p>

Unified AI memory and context system: four storage layers (filesystem L0–L2,
bi-temporal graph, vector/KV, reflect/synthesis), an ECL ingestion pipeline, a
curator validation gate, and an MCP-compatible agent tool API. Runs in Docker
identically on a Raspberry Pi 4 (8 GB) and on a multi-tenant server. License:
Apache-2.0.

## Architecture at a glance

One unified namespace — `stratum://<bereich>/<tenant>/<pfad>` — over four layers:

- **Filesystem (L0–L2)** — Markdown + YAML frontmatter, Git-versioned vault; L0
  abstraction, L1 short summary, L2 full text.
- **Bi-temporal graph** — facts/entities/edges with a validity axis
  (`valid_from`/`valid_to`) and a transaction axis (`tx_from`/`tx_to`);
  corrections **close-before-insert** (at most one open fact per
  `(tenant, subject, predicate)`).
- **Vector/KV** — pgvector embeddings (HNSW) and an add-only dedup classifier
  (`ADD`/`UPDATE`/`DELETE`/`NOOP`, cosine threshold `0.92`).
- **Reflect/synthesis** — a budgeted batch worker that condenses facts into
  synthesis/skill **candidates** (never active writes).

Three processing stages surround the layers: **ECL ingestion** (Extract →
Cognify → Load) writes `staging_candidates`; the **Curator** is the only bridge
from staging into the active tables (seven criteria, §8); the **MCP tool API**
(`write`, `query`, `get`, `promote`, `archive`, `forget`, `link`,
`session_append`, `reflect`, `health`) is the agent surface. Every write appends
exactly one `memory_events` row (append-only truth journal).

Everything is Python 3.12 + PostgreSQL 18 + pgvector — no separate graph DB, no
GPU. Tenant isolation is enforced by Postgres Row-Level-Security via the
non-owner app role `stratum_app`.

## Layout

```
src/stratum/        core package
  uri.py            stratum:// namespace
  db.py             connection / RLS layer
  config.py         Settings (env: STRATUM_*)
  filesystem.py     L0–L2 vault + Git
  graph.py          bi-temporal graph
  vector.py         pgvector + KV + dedup
  llm.py            LLM/embedding client
  ingest.py         ECL pipeline + session store
  curator.py        staging → active gate
  reflect.py        reflect batch worker
  query.py          hybrid query engine
  worker.py         job queue (SKIP LOCKED)
  auth.py           API keys + scopes
  observability.py  health / metrics / JSON logs
  cli.py            `stratum` console script
  mcp/              FastMCP server + tool API
init/               DB bootstrap SQL (extensions, roles, schema, RLS)
deploy/pi/          Raspberry Pi 4 profile (SSD, swap/zram, limits)
deploy/server/      Multi-tenant server profile (TLS/Caddy)
scripts/            backup / restore (WP-15)
benchmarks/         LoCoMo / LongMemEval harness (WP-16)
tests/              pytest suite (DB-free + `integration`)
docs/               ADRs, API reference, operations handbook, backup/restore
```

## Install & test (development)

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
# python -m venv .venv && source .venv/bin/activate # Linux/macOS
pip install -e ".[dev]"
pytest -q
```

The default suite is **DB-free and network-free**: every module takes injected
collaborators, and the tests drive them with deterministic in-memory fakes.

```bash
# DB-free suite only (recommended locally)
pytest -q -o addopts="" -m "not integration"

# end-to-end scenario (write → curate → query → reflect → forget)
pytest tests/test_e2e.py -q -o addopts=""

# coverage of the core package (target: ≥ 80 %)
pytest -q -o addopts="" -m "not integration" --cov=stratum --cov-report=term-missing
```

Real-Postgres tests are marked `integration` and **skipped** unless
`STRATUM_TEST_DB` points at a live pgvector Postgres (they create the schema,
roles and RLS, then exercise close-before-insert and tenant isolation):

```bash
STRATUM_TEST_DB="postgresql://stratum@localhost:5432/stratum" \
  pytest -m integration -q -o addopts=""
```

## Deployment

### Raspberry Pi 4 / 8 GB (resident, Streamable-HTTP)

```bash
# 1. One-time host provisioning (SSD layout, Docker, swap/zram, secrets dir)
sudo bash deploy/pi/setup-pi.sh

# 2. Secrets (never in env files, never in git)
mkdir -p secrets && chmod 700 secrets
openssl rand -base64 32 | tr -d '\n' > secrets/pg_password    # owner role 'stratum'
openssl rand -base64 32 | tr -d '\n' > secrets/app_password   # app role 'stratum_app'
printf '%s' 'YOUR_LLM_API_KEY'        > secrets/llm_key
chmod 600 secrets/*

# 3. Start (always both compose files)
docker compose -f docker-compose.yml -f deploy/pi/docker-compose.override.yml up -d
```

After the first start, set the app-role password:

```bash
docker compose exec stratum-db psql -U stratum -d stratum \
  -c "ALTER ROLE stratum_app PASSWORD '...';"
```

Details, expected `docker stats`, OOM/thrashing fixes: `deploy/pi/README.md`.

### Server / multi-tenant (TLS)

```bash
export STRATUM_DOMAIN=memory.example.com
docker compose -f docker-compose.yml -f deploy/server/docker-compose.override.yml up -d
```

`vector(1024)` instead of `halfvec(1024)`, larger Postgres limits, and Caddy TLS
in front of `stratum-core:8088`. See `deploy/server/README.md`.

## Connecting an MCP client

- **Streamable-HTTP** (resident Pi/Server): point the client at
  `http://127.0.0.1:8088/mcp` (Pi) or `https://$STRATUM_DOMAIN/mcp` (server).
  The process binds `0.0.0.0:8088` inside the container; host-side exposure is
  restricted to localhost by the `127.0.0.1:8088:8088` port mapping.
- **stdio** (local developer): let the client launch the server process:

  ```bash
  python -m stratum.mcp --transport stdio
  # or, inside the compose network:
  docker compose run --rm -i stratum-core python -m stratum.mcp --transport stdio
  ```

The ten tools, their scopes and error codes: **`docs/api-reference.md`**.

## Operator dashboard

A small, dependency-free single-page console ships with the MCP HTTP transport
(no CDN, no build step):

- **http://127.0.0.1:8088/dashboard** (or `https://$STRATUM_DOMAIN/dashboard` behind TLS)

It shows health/counts, areas, browse + L0/L1/L2 reader, hybrid search, the
staging gate, the append-only event journal and two actions (`promote`,
`reflect`). The HTML shell, `/health` and `/metrics` are public;
`/dashboard/api/*` reuses the MCP auth — with `STRATUM_REQUIRE_API_KEY=1` set the
`key` button in the top bar stores a `Bearer stk_…` token. Full endpoint list
(`curl` examples), a screenshot description and troubleshooting:
**`docs/dashboard.md`**.

## Configuration

All settings come from environment variables with the `STRATUM_` prefix
(`src/stratum/config.py`): DB host/user/name, `STRATUM_VECTOR_TYPE`
(`halfvec`|`vector`), MCP transport/host/port, `STRATUM_KB_PATH`, curator
thresholds (`STRATUM_DEDUP_THRESHOLD`, `STRATUM_MIN_CONFIDENCE`,
`STRATUM_CURATOR_MAX_PROMOTIONS_PER_RUN`) and reflect budgets
(`STRATUM_REFLECT_BUDGET_TOKENS`, `STRATUM_REFLECT_MAX_TOKENS`). Secrets are read
from files (`*_FILE`), never hard-coded.

## Documentation

| Document | Contents |
|---|---|
| `MASTER-PLAN.md` | Full, frozen specification (§1–§16) |
| `docs/adr/` | Architecture decision records |
| `docs/api-reference.md` | The ten MCP tools: signatures, scopes, error codes |
| `docs/dashboard.md` | Operator dashboard: URL, auth, JSON API (`curl`), troubleshooting |
| `docs/operations-handbuch.md` | Deployment, health, logs, reflect, rollback, troubleshooting |
| `docs/backup-restore.md` | Backup/restore, retention, RTO/RPO, restore drill |
| `deploy/pi/README.md`, `deploy/server/README.md` | Deployment profiles |
| `CONTEXT.md` | Ubiquitous language |
| `PROGRESS.md` | Build status per work package |

## License

Apache-2.0. Only conceptual reuse from AGPL/unlicensed projects; no code copied
(see `docs/adr/0008-license-policy.md`).