# ADR-0007: MCP-Agent-Tool-API & Transportmodi

**Status:** accepted

## Kontext
Agenten (Claude Code, Cursor, Codex, Hermes) sollen Memory lesen/schreiben. Ein residenter Container
kann keinen stdio-Transport anbieten (stdio setzt voraus, dass der Client den Serverprozess startet).

## Entscheidung
- Tool-Oberfläche (MCP): `write`, `query`, `get`, `promote`, `archive`, `forget`, `link`,
  `session_append`, `reflect`, `health`.
- **Pi/Server resident**: Streamable-HTTP; Prozess bindet im Container an `0.0.0.0:8088`,
  localhost-Beschränkung host-seitig über `ports: 127.0.0.1:8088:8088`.
- **stdio**: client-gestartet, `docker compose run --rm -i stratum-core python -m stratum.mcp ...`
  (im Projektnetz; nicht `--network host`).
- Auth: API-Keys (Argon2id), Scopes `read|write|promote|admin`.

## Konsequenzen
- Sämtliche Clients bedient; keine Portverwirrung zwischen Host- und Container-Bindung.
