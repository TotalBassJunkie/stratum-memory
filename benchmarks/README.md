# Stratum-Memory Benchmark-Harness (WP-16)

Reproduzierbare Evaluation der Query-Engine (`stratum.query.QueryEngine`) gegen die
Langzeit-Memory-Benchmarks **LoCoMo** und **LongMemEval** sowie eine eingebaute
**Temporal-Suite** versionierter Fakten. Metriken und Abnahmeschwellen stammen aus
`MASTER-PLAN.md` §14.

## Reproduzierbarkeit

- **Fester Seed** (`--seed`, Default `0`); jeder der `n_runs`-Läufe nutzt `seed + i`.
- **Gepinnte Modellversion** aus `Settings` (`llm_chat_model`, `llm_embed_model`) — wird im
  Artefakt mitgeschrieben.
- **`n_runs` (Default 3) → Median** über die Läufe.
- **Offline-Default**: `STRATUM_BENCHMARK_OFFLINE=1` verbietet jeden Netzwerkzugriff; die
  Loader lesen lokale Dateien. Ohne Netz laufen nur Fixtures (`--dataset temporal`).

## Datensätze beziehen

LoCoMo und LongMemEval sind öffentlich und dürfen für interne Evaluation genutzt werden
(§16.9). Beide werden **lokal** als `.json`/`.jsonl` erwartet (Pfad über `--path`).

- **LoCoMo** — `github.com/snap-research/locomo` (`data/locomo10.json`): Konversationen mit
  QA-Paaren; der Loader projiziert jede QA in einen Fall (Kategorie = `category`, Gold =
  `answer`). Die Rohtexte bleiben unter `metadata.conversation` erhalten, damit sie vor dem
  Lauf ingestiert werden können.
- **LongMemEval** — `huggingface.co/datasets/xiaowu0162/longmemeval` (`longmemeval_s.json`):
  Langzeit-Memory-Aufgaben; Kategorie = `question_type` (`knowledge_update`,
  `temporal_reasoning`, `multi_session`, `single_session`). Enthält ein Fall ein
  Referenzdatum (`question_date`), wird `as_of_valid` gesetzt.

Optionaler Download (nur mit `--allow-download` **und** ohne `STRATUM_BENCHMARK_OFFLINE=1`)
nutzt die in `benchmarks/datasets.py::DOWNLOAD_URLS` hinterlegten Raw-URLs; ein Fehlschlag
bricht ab, statt still das Netz zu benutzen.

## Eingebaute Temporal-Suite (Fixture)

`--dataset temporal` braucht keine Datei. `TEMPORAL_FACTS` versioniert den Fakt
`backend.ip_address`: `10.0.0.5` → korrigiert zu `10.0.0.9` (Valid- **und** Tx-Zeit). Daraus
entstehen echte `as_of_valid`/`as_of_tx`-Fälle:

| Fall | Frage | Gold | Achse |
|---|---|---|---|
| `temporal-ip-valid-old` | backend ip_address | `10.0.0.5` | `as_of_valid` 2024-03 |
| `temporal-ip-valid-new` | backend ip_address | `10.0.0.9` | `as_of_valid` 2024-09 |
| `temporal-ip-tx-before-correction` | backend ip_address | `10.0.0.5` | `as_of_tx` 2024-04 |
| `temporal-ip-tx-after-correction` | backend ip_address | `10.0.0.9` | `as_of_tx` 2024-07 |
| `temporal-ip-current` | backend ip_address | `10.0.0.9` | aktuell |
| `temporal-port-current` | backend port | `8080` | aktuell |
| `temporal-deploy-target-removed` | deploy_target hostname | *(keiner)* | geschlossen |

Der `stale_value` eines Falls darf **nicht** auftauchen; jeder Treffer zählt als
Contradiction.

### Curator-Durchlauf (post_curator)

§14 fordert „Contradiction-Rate = 0 **nach Curator**". Wird dem Harness ein
`curator` injiziert, laufen die versionierten `TEMPORAL_FACTS` **vor** den
Temporal-Proben durch `Curator.validate` (und bei Zustimmung `promote`); die
gemeldete Contradiction-Rate ist dann eine echte **Nach-Curator**-Messung und das
Artefakt setzt `"post_curator": true`.

Ohne injizierten Curator kann nichts kuratiert werden; die Zahlen sind dann eine
**Vor-Curator**-Messung und das Artefakt setzt `"post_curator": false`. Das Feld
macht die Aussage des Artefakts eindeutig — ein `false` darf nicht als
Nach-Curator-Ergebnis gelesen werden.

### Ausführen gegen echte Engine erfordert Seeding

`--dataset temporal` misst **nur dann** etwas Reales, wenn die versionierten
Fakten vorher in den Store geschrieben wurden. `benchmarks/datasets.py::
seed_temporal_facts(target, facts=None, tenant="default")` schreibt
`TEMPORAL_FACTS` bi-temporal exakt (Valid- **und** Tx-Fenster) über eine
injizierte Graph-/Store-API:

- bevorzugt `target.seed_facts(facts, tenant=...)` (Batch-Hook; erhält die vollen
  Zeilen inklusive **Tx-Fenster**, schreibt also beide Achsen exakt), sonst
- `target.upsert_entity(...)` + `target.add_fact(...)` (die
  `stratum.graph.TemporalGraph`-Oberfläche; jedes `subject` wird zur Entity). Hinweis:
  `TemporalGraph.add_fact` hat keine `tx_from`/`tx_to`-Parameter (es setzt
  `tx_from = now()`), daher reproduziert dieser Pfad das **Valid-Fenster** exakt; für ein
  exaktes **Tx-Fenster** ist der `seed_facts`-Hook zu verwenden.

Ohne Seeding ist die Temporal-Suite gegen eine echte Engine **undefiniert**
(leerer Store ⇒ leere Antworten).

## Ausführen

```powershell
# Temporale Fixture, 3 Läufe, Median berichten
.\.venv\Scripts\python.exe -m benchmarks.run --dataset temporal --runs 3 --out benchmarks/report-2026-10-11.json

# LoCoMo lokal
.\.venv\Scripts\python.exe -m benchmarks.run --dataset locomo --path data/locomo10.json --runs 3 --baseline

# Vektor-Baseline zur Abnahmeschwelle
.\.venv\Scripts\python.exe -m benchmarks.run --dataset temporal --runs 3 --baseline --out benchmarks/report-baseline.json
```

Optionen: `--dataset {locomo,longmemeval,temporal}`, `--path`, `--runs`, `--seed`, `--k`,
`--level {l0,l1,l2}`, `--baseline`, `--out`, `--allow-download`.

## Artefakt

`--out` (Default `benchmarks/report-<datum>.json`) enthält:

```json
{
  "dataset": "temporal",
  "seed": 0,
  "model": "gpt-4o-mini",
  "embedding_model": "text-embedding-3-small",
  "n_runs": 3,
  "timestamp": "2026-10-11T...Z",
  "post_curator": false,
  "metrics": { "f1": ..., "exact_match": ..., "accuracy": ...,
               "temporal_correctness": ..., "contradiction_rate": ... },
  "per_category": { "temporal_valid": {...}, "temporal_tx": {...}, ... },
  "predictions": [ { "case_id": ..., "gold": ..., "prediction": ...,
                     "error": false, "temporal_ok": ... } ],
  "runs": [ { "overall": {...}, "per_category": {...} }, ... ],
  "baseline": { "config": {...}, "metrics": {...}, "per_category": {...} },
  "settings": { "llm_chat_model": ..., "dedup_threshold": 0.92, ... }
}
```

## Metriken

`benchmarks/metrics.py` (rein, deterministisch, token-basiert):

- `f1(pred, gold)` — Token-Overlap-F1.
- `exact_match` / `accuracy` — normalisierte Gleichheit (SQuAD-Stil).
- `temporal_correctness` — Gold als Token-Slice in der Antwort **und** keine Contradiction.
- `contradiction_rate` — Anteil Antworten, die einen stale (geschlossenen) Wert zeigen.
- `ndcg_at_k`, `recall_at_k` — Ranking-Qualität.

## Abnahmeschwellen (§14)

1. **Stratum schlägt die Vektor-Baseline auf den Temporal-Kategorien.** Die Baseline ist
   `VECTOR_BASELINE` (`mode="vector"`, `level="l0"`); mit `--baseline` wird sie im Artefakt
   mitgeschrieben. Zu vergleichen sind die Kategorien `temporal_valid`, `temporal_tx`,
   `knowledge_update`, `contradiction` (`run.TEMPORAL_CATEGORY_PREFIXES`).
   `temporal_correctness` der Engine **>** der Baseline (mindestens eine der Temporal-
   Kategorien **strikt** größer, nicht nur gleich).
2. **Kein Regressionsanstieg > 2 %** zwischen Releases. Jede Metrik (`f1`, `accuracy`,
   `temporal_correctness`, `contradiction_rate`) darf gegenüber dem Vor-Release um
   **maximal 2 Prozentpunkte** fallen (`contradiction_rate` steigen). Artefakte sind die
   Vergleichsbasis (`report-<datum>.json`). Nur Artefakte mit `"post_curator": true` sind
   für diese Schwelle gültig.
3. **Contradiction-Rate = 0 nach Curator** auf der Temporal-Suite. Erfüllt nur, wenn das
   Artefakt `"post_curator": true` ausweist (siehe „Curator-Durchlauf").

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_benchmarks.py -q -o addopts=""
```

Die Tests sind DB-frei und ohne Netz: Metriken mit bekannten Werten, Loader parsen die
Fixture, die Temporal-Suite enthält korrigierte Fakten, der Harness ist deterministisch
(zwei Läufe identisch), `run.py` schreibt die Artefaktstruktur (via Fixture) und die
Vektor-Baseline-Config ist vorhanden.
