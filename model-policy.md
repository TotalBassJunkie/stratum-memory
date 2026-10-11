# Model-Policy: Stratum-Memory

Verbindliche Rollen- und Modellzuordnung für die autonome Umsetzung. Grundregel **L8**: Wer baut,
inspiziert nicht — der Prüfer stammt aus einer **anderen Modellfamilie** als der Builder.

## Rollen (logisch, aus dem Master-Plan)

| Rolle | Aufgabe | Familie (Plan) | Verfügbares Modell (Ist, primär, FREE) | Fallback (paid, günstig) |
|---|---|---|---|---|
| PLANNING | Arbeitspaket-Plan erstellen | DeepSeek (r1:14b) | `aihubmix/coding-glm-5.3-free` | `merge-gateway/deepseek/deepseek-v4.1-flash` |
| PLAN_REVIEW | Plan unabhängig prüfen | Qwen (qwen3:14b) | `aihubmix/qwen3.8-flash` | `merge-gateway/qwen/qwen3.8-flash` |
| IMPLEMENTATION | Code bauen | (Coder) | `aihubmix/coding-glm-5.3-free` | `aihubmix/coding-minimax-m2.7-free` |
| IMPLEMENTATION_REVIEW | Implementierung unabhängig prüfen | Qwen (qwen3:14b) | `aihubmix/qwen3.8-flash` | `merge-gateway/qwen/qwen3.8-flash` |

**Kostenprinzip:** Zuerst kostenlose Modelle (bevorzugt `aihubmix`-Free-Tier:
`coding-glm-5.3-free`, `coding-glm-5.3-flash-free`, `coding-minimax-m2.7-free`,
`xiaomi-mimo-v2.5-free`), bei Nichtverfügbarkeit die günstigen Paid-Fallbacks.

**L8-Regel (Builder ≠ Prüfer-Familie) hat Vorrang:** Builder = **GLM/MiniMax**, Reviewer = **Qwen**
(stets andere Familie). Der Plan-Reviewer (Qwen) prüft die Plan-Ausgabe des Planers (GLM/DeepSeek)
— ebenfalls familienfremd. Der Koordinator (DeepSeek) misst Beweise selbst nach.

## L8-Absicherung (Ist-Stand)

- Builder = **Qwen**-Familie (`qwen3-coder-next`).
- Implementierungs-Review = **Anthropic**-Familie (`claude-sonnet-5-5`) → andere Familie, L8 erfüllt.
- Plan-Review = **Qwen** (`qwen3.8-flash`) prüft Pläne des **DeepSeek**-Planers → andere Familie, L8 erfüllt.
- Koordinator (dieser Agent, DeepSeek) misst Beweise selbst nach (Testläufe), committet und führt den Handoff.

## Abweichung vom Master-Plan-Roster

Das im Master-Plan genannte Ollama-Roster (`.14b`-Modelle) ist in dieser Umgebung nicht verfügbar
(keine lokale GPU-Inferenz auf diesem Host). Es wird **nach Familien** auf verfügbare Provider
abgebildet; die L8-Trennung bleibt gewahrt. Diese Abweichung ist in Abschnitt 16 des Master-Plans
als „gegeben angenommen" abgedeckt (externer LLM-Zugriff statt lokaler Inferenz).

## Gate-Pflicht

Jedes Arbeitspaket gilt erst als abgeschlossen, wenn:
1. Plan erstellt (PLANNING) und unabhängig geprüft (PLAN_REVIEW) → Verdict APPROVED,
2. Implementierung gebaut (IMPLEMENTATION),
3. Implementierung unabhängig geprüft (IMPLEMENTATION_REVIEW) → Verdict APPROVED,
4. Koordinator die Beweise selbst nachgemessen hat (Tests grün) und committet wurde.
