# Pre-implementation audit — Multilingual AI Response Engine

Conducted before writing any code, to establish where a language layer can be
inserted without touching retrieval, scoring, RAG, the vault, temporal memory
or the scheduler.

## 1. Where every AI response is actually produced

`ResearchAnalyst.run()` → `ResearchAnalyst._finalise()` is a **single funnel**.
Every capability, every chat turn, every report section and the section writer
all pass through it. `_finalise` is where `audit()`, `check()`, `enforce()` and
`annotate()` already run.

**Consequence:** one insertion point in `_finalise` reaches 100% of generated
prose. No per-capability work, no report-orchestrator changes.

## 2. Retrieval is already script-tolerant — but not cross-lingual

`app/services/retrieval/engine.py` was written with this in mind:

```python
_TERM = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)
```

and the Postgres text-search configuration is `simple`, not `english`, with a
comment stating it is so "Devanagari and transliterated Hinglish survive
tokenisation".

So Devanagari **tokenises** correctly today. What does not happen is
**matching**: the corpus is English, so `राजस्व` shares no token with
"revenue" and retrieves nothing. This is the RETR-class limitation already
recorded — Hindi retrieval measured 0.0026 MRR in the Retrieval 2.1 benchmark.

**Consequence:** cross-language search must be solved by normalising the
*query* to English **before** it reaches the retriever. That is an inbound
Language Adapter concern sitting *above* retrieval. Zero retrieval code
changes, zero new vectors, zero new chunks — which is exactly what the brief
demands.

## 3. Citation markers are structural and must survive translation

The model emits `[revenue]`; `annotate()` rewrites it to `[Revenue]` against
the citation index. A translator that touches those brackets breaks the
citation audit, the guardrail check and the evidence chain simultaneously.

**Consequence:** translation must run on *masked* text with every protected
token replaced by an opaque sentinel, then restored. This is the single
highest-risk part of the feature.

## 4. `User.preferences` already exists

`app/models/platform.py:176` — `preferences: Mapped[dict | None] = mapped_column(JSON)`.

**Consequence:** "remember user preference for future sessions" needs **no
migration**, satisfying the brief's no-migration constraint outright.

## 5. Scores are computed before any language exists

`AIScoreResult` is a frozen dataclass of pure arithmetic; `AIScoringService`
never sees a language. The same holds for `HybridRetrievalEngine`, the vault,
temporal memory and the scheduler.

**Consequence:** score/citation/evidence consistency across languages is
guaranteed *structurally* rather than by testing — but it will be tested
anyway, because a structural guarantee that nobody verified is an assumption.

## 6. Insertion points chosen

| Layer | Change | Risk |
|---|---|---|
| `domain/language/` | New. Detection, glossary, protection rules | None — new code |
| `services/language/` | New. Adapter + pluggable translators | None — new code |
| `ai/analyst.py` | Two hooks: normalise query in, adapt response out | Low — additive, defaults to English |
| `ai/prompt_builder.py` | Optional target-language instruction | Low — no-op when English |
| `schemas/ai.py` | `language` request field, `language` response block | **Backward compatible** — optional with default |
| `api/v1/ai.py` | Pass language through; new `/ai/languages` | Additive |
| `api/v1/auth.py` | Persist preference to existing JSON column | Additive, no migration |
| Retrieval / scoring / RAG / vault / memory / scheduler | **UNCHANGED** | — |
