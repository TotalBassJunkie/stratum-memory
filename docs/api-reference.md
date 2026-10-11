# Stratum-Memory — MCP Tool API Reference

Die zehn Agent-Tools aus `MASTER-PLAN.md` §6, abgeglichen mit der tatsächlichen
Implementierung in `src/stratum/mcp/tools.py` (reine Funktionen) und
`src/stratum/mcp/server.py` (FastMCP-Registrierung).

Grundregeln:

- **Auth**: Im **Standardbetrieb** läuft der Server mit einem transparenten
  **Bootstrap-Kontext** (`admin`, Tenant `default`) — kein Key nötig, gedacht für
  den lokalen stdio-Betrieb. Optional lässt sich per
  `STRATUM_REQUIRE_API_KEY=1` ein **Bearer-Key-Gate** aktivieren (nur für den
  Streamable-HTTP-Transport): dann trägt jeder Aufruf `Authorization: Bearer
  stk_<tenant>_<random>` (Argon2id-Hash in `api_keys`, geprüft über
  `stratum.auth.verify_key`). Der Tenant stammt aus dem Auth-Kontext
  (`AuthContext.tenant`) — nicht aus dem Tool-Argument. Fehlt der Header → `401`,
  ungültiger/widerrufener Key → `403`.
- **Scope zuerst**: Jedes Tool prüft seinen Scope, **bevor** ein Collaborator
  berührt wird. Fehlt der Scope → `FORBIDDEN`. Die Zuordnung steht in
  `SCOPE_FOR_TOOL`:

  | Scope | Tools |
  |---|---|
  | `read` | `query`, `get`, `health` |
  | `write` | `write`, `session_append`, `link` |
  | `promote` | `promote`, `archive`, `forget`, `reflect` |

  `admin` impliziert alle Scopes und darf tenant-übergreifend arbeiten.
- **Rückgaben**: JSON-Objekte. Zeitstempel ISO-8601 (UTC).
- **Fehlerformat** (nie eine Exception, immer ein Dict):

  ```jsonc
  { "error": { "code": "NOT_FOUND", "message": "…" } }
  ```

  Codes (`ErrorCode`): `NOT_FOUND` · `FORBIDDEN` · `VALIDATION` · `CONFLICT` ·
  `BUDGET_EXCEEDED` · `INTERNAL`. Ein unerwarteter Fehler wird zu `INTERNAL` mit
  der generischen Meldung `"internal error"` (Details nur im Log).
- **Transports**: stdio (Client startet den Prozess) oder Streamable-HTTP. Die
  Tool-Signaturen sind in beiden identisch; die hier gezeigten Argumente sind die
  öffentlichen (der `ToolContext` ist intern und wird vom Server gebunden).
- **HTTP-Betriebsproben** (Streamable-HTTP, öffentlich — kein Key): `GET /health`
  (Readiness-JSON, `200` bei `status=="ok"`, sonst `503`) und `GET /metrics`
  (Prometheus-Textformat `text/plain; version=0.0.4`).

`level ∈ {l0, l1, l2}` · `mode ∈ {hybrid, vector, graph, kv}`.

---

## 1. `stratum_write`

Inhalt durch Ingestion (ECL) + Staging schreiben; optional sofort promovieren.

```python
stratum_write(
    uri: str,                             # stratum://<bereich>/<tenant>/<pfad>
    content: str,                         # Markdown/JSON
    kind: str,                            # "doc" | "fact" | "episode" | "skill"
    metadata: object | None = None,       # tags, valid_from, source_uri, …
    auto_promote: bool = False,           # true nur für vertrauenswürdige Aufrufer
) -> {
    uri: str,
    status: "staged" | "promoted",
    l0: str,
    candidate_id: str | None,
    ingest_id: str | None,
    chunks_new: int | None,
    facts_staged: int | None,
    truncated: bool | None,
    metadata: object,
    promote: object | None,               # Curator-Ergebnis bei auto_promote
}
```

- **Scope**: `write`. Bei `auto_promote=True` **zusätzlich** `promote` — sonst
  `FORBIDDEN`, noch bevor der Curator angefasst wird.
- **Ablauf**: Die Pipeline zerlegt `content` in Chunks (Level-1-Dedup per
  `content_hash`), bettet neue Chunks ein und extrahiert für `kind ∈ {fact, doc}`
  bi-temporale Fakten als `staging_candidates(status='pending')`. `write` selbst
  **promoviert nie** (das ist der Curator). Der `l0`-Wert stammt aus
  `make_l0(content)`.
- **`auto_promote`**: Der Curator promoviert den gestagten Kandidaten. Liefert die
  Pipeline eine `candidate_id`, wird genau dieser Kandidat promoviert; sonst fällt
  der Curator auf einen tenant-weiten `promote_batch` zurück (Batch-Zähler).
- **Fehler**: `FORBIDDEN` (Scope), `VALIDATION` (unbekanntes `kind`, leerer
  `uri`, `content` kein String), `INTERNAL`.

---

## 2. `stratum_query`

Hybrid-Suche über die vier Speicher-Ebenen mit Zeit- und Ebenen-Kontrolle.

```python
stratum_query(
    query: str,
    scope: str | None = None,             # URI-Präfix, z.B. "stratum://docs/default/projects"
    mode: "hybrid"|"vector"|"graph"|"kv" = "hybrid",
    k: int = 8,                           # >= 1
    level: "l0"|"l1"|"l2" = "l0",
    as_of_valid: str | None = None,       # Weltzeit-Sicht
    as_of_tx: str | None = None,          # Systemzeit-Sicht
    filters: object | None = None,        # {tags, bereich, confidence_min}
) -> {
    results: [ { uri, level, score, content, provenance, valid_from?, valid_to? } ],
    count: int,
    mode: str,
    level: str,
}
```

- **Scope**: `read`.
- **`mode`**: `vector` (Chunks + ähnliche Fakten), `graph` (Fakten/Edges ab der
  aufgelösten Startentität, zeitgefiltert), `kv` (Präfix/Exakt-Schlüssel),
  `hybrid` (Reciprocal Rank Fusion, `k=60`, deterministisch).
- **`scope`** ist ein **Pfad-Präfix** und verengt nur die Ergebnismenge; er
  erweitert **nie** den Tenant-Zugriff (die Engine liest immer unter dem
  Auth-Tenant).
- **Scoring** ist immer in `[0,1]`, Gleichstand bricht auf `uri` aufsteigend.
- **Fehler**: `FORBIDDEN`, `VALIDATION` (leere `query`, unbekanntes `mode`/`level`,
  `k < 1`), `INTERNAL`.

---

## 3. `stratum_get`

Direktzugriff auf eine URI.

```python
stratum_get(uri: str, level: "l0"|"l1"|"l2" = "l1") -> {
    uri: str, level: str, content: str,
    metadata: object,
    valid_from: str | None, valid_to: str | None,
}
```

- **Scope**: `read`. Der Tenant wird aus der `uri` abgeleitet.
- **Quelle**: der Filesystem-Vault (`FilesystemStore.read`), der L0/L1/L2
  besitzt. `l0` = Abstraktion, `l1` = Kurzfassung, `l2` = Volltext.
- **Fehler**: `NOT_FOUND` (kein Dokument), `VALIDATION` (ungültiges `level` oder
  URI), `FORBIDDEN`, `INTERNAL`.

---

## 4. `stratum_promote`

Einen Staging-Kandidaten in den Aktivbestand freigeben.

```python
stratum_promote(candidate_id: str, override_reason: str | None = None) -> {
    uri: str | None,
    status: "promoted" | "rejected",
    event_id: str | None,
    action: str | None,                   # ADD|UPDATE|DELETE|NOOP|PROMOTE
    reason: str | None,                   # Ablehnungsgrund bei "rejected"
    candidate_id: str,
    override_reason: str | None,
}
```

- **Scope**: `promote`.
- **Delegiert** an `Curator.promote`: 7 Kriterien (§8), close-before-insert bei
  `UPDATE`, ein `memory_events`-Eintrag je Entscheidung, idempotent (ein bereits
  entschiedener Kandidat wird unverändert zurückgegeben). Das Tool normalisiert
  den Curator-Status `approved` → `promoted`; der öffentliche Status ist nur
  `promoted` | `rejected`.
- `override_reason` wird nur auf dem Ergebnis vermerkt; die Validierung wird
  **nie** umgangen.
- **Fehler**: `NOT_FOUND` (unbekannte `candidate_id`), `VALIDATION` (leere id),
  `FORBIDDEN`.

---

## 5. `stratum_archive`

Aus dem Aktivbestand nehmen, Historie behalten (weich).

```python
stratum_archive(uri: str, reason: str) -> {
    uri: str, status: "archived", event_id: str, reason: str,
}
```

- **Scope**: `promote`.
- Schreibt ausschließlich einen `ARCHIVE`-`memory_events`-Eintrag. Es wird
  **nichts gelöscht** (keine Zeilen, Vektoren oder Dateien).
- **Fehler**: `VALIDATION` (fehlender `reason`, ungültige URI), `FORBIDDEN`.

---

## 6. `stratum_forget`

Recht auf Vergessen.

```python
stratum_forget(uri: str, hard: bool = False, reason: str) -> {
    uri: str, mode: "soft" | "hard",
    deleted_refs: int, git_commit: str | None,
    event_id: str, reason: str,
}
```

- **Scope**: `promote`.
- `hard=False` (soft) archiviert nur — keine physische Löschung.
- `hard=True` löscht in **einer** Transaktion, in Abhängigkeitsreihenfolge
  (`edges` → `facts` → `entities` → `chunks`/`files`/`uri_registry`), entfernt die
  Vault-Datei mit Git-Commit `forget: <uri>` und zählt `deleted_refs`.
- In beiden Modi wird ein `FORGET`-`memory_events`-Eintrag geschrieben.
- **Fehler**: `VALIDATION` (fehlender `reason`, ungültige URI), `FORBIDDEN`.

---

## 7. `stratum_link`

Explizite bi-temporale Graphkante setzen.

```python
stratum_link(
    src_uri: str, dst_uri: str, rel_type: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    confidence: float = 1.0,
) -> { edge_id: str, fact_id: str, src_uri: str, dst_uri: str, rel_type: str }
```

- **Scope**: `write` — geprüft für **beide** Endpunkte (`src_uri` **und**
  `dst_uri`); eine Cross-Tenant-`dst_uri` ist `FORBIDDEN`.
- Löst/erzeugt die beiden Endpunkt-Entitäten, schreibt einen Fakt für die Relation
  und eine gerichtete, faktgestützte Kante (`edges.fact_id`).
- **Fehler**: `VALIDATION` (leerer `rel_type`, ungültige URI), `FORBIDDEN`,
  `INTERNAL`.

---

## 8. `stratum_session_append`

Episodisches Kurzzeitgedächtnis fortschreiben.

```python
stratum_session_append(
    session_id: str, role: str, content: str,
    metadata: object | None = None,
) -> { uri: str, turn: int, session_id: str }
```

- **Scope**: `write`.
- Delegiert an `SessionStore.append`: Die Turns liegen als JSONB-Liste unter dem
  KV-Schlüssel `episode:<session_id>` und werden als Markdown-Episodendatei
  (`stratum://episodes/<tenant>/<yyyy>/<mm>/<session_id>`) gespiegelt.
- `turn` ist der Index des neuen Turns (0-basiert). Das ist **keine** aktive
  Memory-Schreiboperation und erzeugt **kein** `memory_events`.
- **Fehler**: `VALIDATION` (leere `session_id`/`role`), `FORBIDDEN`, `INTERNAL`.

---

## 9. `stratum_reflect`

Synthese-Lauf anstoßen (kostenbegrenzt).

```python
stratum_reflect(
    scope: str | None = None,
    budget_tokens: int = 20000,
    async_: bool = True,
) -> { job_id: str, scope: str, estimated_tokens: int }
```

- **Scope**: `promote`.
- Validiert `budget_tokens` gegen `Settings.reflect_max_tokens` (Default 50 000);
  darüber → `BUDGET_EXCEEDED`. Reiht einen `kind='reflect'`-Job in die `jobs`-Tabelle
  ein (`JobQueue.enqueue`).
- `scope` defaultet auf den Tenant. Der eigentliche Lauf ist Aufgabe des
  Workers (`ReflectWorker.run`), der Ergebnisse **nur** als `pending`-Kandidaten
  (`synthesis`/`skill`) schreibt.
- **Fehler**: `VALIDATION` (`budget_tokens < 1`), `BUDGET_EXCEEDED`, `FORBIDDEN`,
  `INTERNAL`.

---

## 10. `stratum_health`

Statusabfrage.

```python
stratum_health() -> {
    status: "ok" | "degraded",
    db: bool, git: bool, llm: bool,
    queued_jobs: int | None,
    version: str | None,
}
```

- **Scope**: `read`.
- Delegiert an die injizierte Probe (Default `stratum.observability.health`), die
  nie eine Exception wirft: DB (`SELECT 1`), Git-Vault-Präsenz, LLM-Base-URL und
  Queue-Tiefe (`jobs` mit `status='queued'`).
- `status` ist `"ok"` genau dann, wenn die DB erreichbar ist, sonst `"degraded"`.
- **Fehler**: `FORBIDDEN`.

---

## Fehlercode-Übersicht

| Code | Bedeutung | Typische Auslöser |
|---|---|---|
| `NOT_FOUND` | Ziel existiert nicht | `get` ohne Datei, `promote` mit unbekannter id |
| `FORBIDDEN` | Scope/Tenant fehlt | fehlender Scope, Cross-Tenant-URI, kein Auth-Kontext |
| `VALIDATION` | Eingabe ungültig | unbekanntes `kind`/`mode`/`level`, leere Pflichtfelder, `k < 1` |
| `CONFLICT` | Konflikt (reserviert) | im Fehlercode-Vertrag reserviert; konkrete Konflikte erscheinen beim `promote` als `status="rejected"` mit `reason` |
| `BUDGET_EXCEEDED` | Kosten-/Tokenlimit | `reflect` über `reflect_max_tokens` |
| `INTERNAL` | Unerwarteter Fehler | fehlender Collaborator, Backend-Fehler (generische Meldung) |

`CONFLICT` ist im Fehlercode-Vertrag reserviert; konkrete Konflikte (Dublette,
Widerspruch) erscheinen beim `promote` als `status="rejected"` mit `reason`
(`duplicate`, `contradiction`, `contradiction_unverifiable`, `low_confidence`,
`no_provenance`, `unsafe`, `tenant_mismatch`, `invalid_time`, `no_embedding`).
`noop` ist **kein** Ablehnungsgrund — die Dedup-Aktion `NOOP` ist ein
Curator-*Action*-Token, kein Reject-Reason.