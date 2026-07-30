# Module 6 — AI Research Analyst

**Scope:** provider abstraction · grounding · versioned prompt library · citation engine ·
guardrails · memory · tools · streaming · caching · cost accounting

**Status: complete and tested.** 861 tests passing (153 new). Awaiting review before Module 7.

---

## 1. AI architecture

The governing principle: **the platform generates the numbers, the LLM explains them.**

```
question
   │
   ▼
ContextBuilder ──── harvests citations from every engine
   │                (statements, ratios, forecast, valuation, scoring)
   ▼
PromptBuilder ───── preamble + declared evidence + memory + task
   │
   ▼
ProviderRouter ──── select → retry → fall back → cache → account
   │
   ▼
CitationEngine ──── verify every [key] resolves; flag uncited numbers
   │
   ▼
Guardrails ──────── classify Fact / Model output / Interpretation / Opinion
   │
   ▼
AnalystResult ───── content + citations + audit + composition + cost
```

Grounding works by **omission, not instruction**. A figure the platform did not compute never
enters the prompt, so the model has nothing to repeat. Asking a model politely not to fabricate
is necessary but insufficient; removing the raw material is structural.

---

## 2. Folder structure

```
backend/app/
├── domain/ai/types.py              Citation, Message, ClaimType, EvidenceKind
└── services/ai/
    ├── providers/
    │   ├── base.py                 transport, retry surface, cost, dotted-path extraction
    │   ├── shapes.py               3 wire-format adapters
    │   ├── openrouter.py openai.py claude.py gemini.py   registry rows only
    │   ├── mock.py                 deterministic offline analyst
    │   └── router.py               selection, retry, fallback, cache, ledger
    ├── context_builder.py          grounding — harvests 60 citations
    ├── prompt_library.py           17 versioned prompts + guardrail preamble
    ├── prompt_builder.py           assembly
    ├── citation_engine.py          verification and annotation
    ├── guardrails.py               claim classification and enforcement
    ├── memory.py                   session state, token-budgeted history
    ├── tools.py                    6 platform lookups
    ├── analyst.py                  orchestration
    └── service.py                  persistence and prompt versioning
```

---

## 3. Provider abstraction

Four vendors, **three adapters** — because a provider is defined by the dialect it speaks, not
its brand. OpenRouter and OpenAI share the `openai` adapter and differ only in registry data.

| Provider | Shape | Auth | Default model |
|---|---|---|---|
| OpenRouter | `openai` | `Authorization: Bearer {key}` | openai/gpt-4o-mini |
| OpenAI | `openai` | `Authorization: Bearer {key}` | gpt-4o-mini |
| Claude | `anthropic` | `x-api-key: {key}｜anthropic-version` | claude-3-5-sonnet |
| Gemini | `gemini` | key in URL | gemini-1.5-pro |

Response extraction is driven by a **dotted path from the registry** (`choices[0].message.content`,
`content[0].text`, `candidates[0].content.parts[0].text`), so a provider with a novel response
shape needs a config row, not an adapter.

**This is the Module 4 defect, corrected.** That audit found the workbook shipped a provider
*table* while its VBA still branched on vendor name. Two tests enforce the fix here: no vendor
endpoint may appear in transport code, and transport may not branch on `config.name`. Both parse
the AST and strip docstrings first, so prose may explain vendors while code may not name them.

**Router behaviour** — retry and fallback are deliberately separate concerns:

- **Retry** — same provider, transient failure (5xx, timeout, 429). Exponential backoff with
  jitter, 3 attempts. A 4xx is *never* retried.
- **Fallback** — next provider, hard failure (no key, non-retryable). Records `fell_back_from`.

Conflating them produces the worst outcome: hammering a dead provider while a healthy one idles.

---

## 4. Prompt library

17 versioned prompts covering all 16 required capabilities plus chat. Stored as **data**: edits
create a *new version* and deactivate the old one — nothing is mutated, so a report generated
last quarter remains reproducible against the prompt that produced it. Rollback is one call.

Every prompt inherits a shared preamble carrying five absolute rules: **grounding, citation,
claim labelling, no advice as certainty, honesty about gaps.**

Each prompt also declares which evidence families it needs, so a moat prompt receives ROIC and
margin history rather than all 60 figures diluted together.

---

## 5. Citation framework

Verification runs *after* generation, because instruction alone is not evidence of compliance.

| Check | Purpose |
|---|---|
| **Resolution** | Every `[key]` must exist in the supplied evidence |
| **Coverage** | Numeric sentences should carry a citation |
| **Fabrication** | Numbers matching nothing in the evidence are surfaced |

Results are **advisory metadata, not a hard block** — suppressing an answer hides the problem,
whereas showing "3 unsupported figures" is actionable. Unknown keys are left visible in the
rendered output rather than silently stripped.

---

## 6. Guardrails

Every paragraph is classified so a reader can see what rests on filings and what on reasoning:

| Class | Meaning |
|---|---|
| **Fact** | Reported figure from the statements |
| **Model output** | Platform-computed forecast, valuation or score |
| **Interpretation** | AI reasoning over the above |
| **Opinion** | Judgement — must be hedged |

Unclassifiable prose defaults to **interpretation**, the cautious choice: mislabelling reasoning
as fact is the more damaging error. Directive advice, certainty language and unhedged opinion
are detected and reported; the disclosure is always appended.

---

## 7. Memory and tools

Memory pins the company **explicitly** rather than re-inferring it each turn, so "what about its
debt?" resolves on the fifth message. Switching company clears assumptions tied to the old one.
History is trimmed by **token budget, not turn count** — three long analyses cost more context
than twenty short questions.

Six tools expose platform lookups (financial, ratio, forecast, DCF, scoring, document search).
They return *citations*, not prose, so anything a tool surfaces is automatically auditable.

---

## 8. API

| Method | Endpoint |
|---|---|
| GET | `/ai/capabilities` · `/ai/providers` · `/ai/usage` · `/ai/prompts` |
| PUT | `/ai/prompts/{key}` — save a new version |
| POST | `/ai/prompts/{key}/activate` — roll back |
| POST | `/company/{ticker}/ai/analyse` · `/ai/chat` · `/ai/chat/stream` · `/ai/report` |
| GET | `/company/{ticker}/ai/context` · `/ai/history` |

`/ai/context` returns exactly what the model may see — the auditability surface.

---

## 9. Test results

```
861 passed in 12.5s
```

| File | Tests |
|---|---:|
| `test_ai_engine.py` | **82** |
| `test_ai_api.py` | **71** |
| *(Modules 1–5)* | 708 |

**Verified:**

| Assertion | Result |
|---|---|
| All three wire formats build correct payloads | ✔ |
| Anthropic hoists system prompt; Gemini renames assistant→model | ✔ |
| **No vendor endpoint hard-coded in transport** | ✔ |
| **Transport never branches on provider name** | ✔ |
| 4xx is not retried; 5xx and 429 are | ✔ |
| Fallback records `fell_back_from` | ✔ |
| Cache prevents a second provider call | ✔ |
| Offline provider cannot emit an uncited number | ✔ |
| Decimal points do not split sentences | ✔ |
| Invented citation keys caught | ✔ |
| Fabricated numbers flagged; paraphrase not | ✔ |
| Claim classification across all four types | ✔ |
| Unclassifiable prose defaults to interpretation | ✔ |
| Switching company clears assumptions | ✔ |
| History trimmed by tokens, not turns | ✔ |
| Prompt edits create versions; rollback works | ✔ |
| Only one active version per prompt | ✔ |
| All 17 capabilities run end to end | ✔ |
| No fabricated citations across 4 companies | ✔ |

### Latency

| Operation | Time |
|---|---:|
| Grounding (60 citations from 5 engines) | 21.9 ms |
| Single analysis | 23.0 ms |
| Chat turn | 23.8 ms |
| 6-section report | 41.0 ms |
| Catalogue endpoints | 2.3–3.8 ms |

Cache hit rate in the benchmark run: **24 of 38 calls**. These isolate *platform* overhead;
network time will dominate with a live provider.

---

## 10. Defects found and fixed

**1. Percentages were handed to the model as fractions.** `Citation.render()` emitted
`WACC: 0.15 %` for a 15.17% figure. An LLM has no way to detect a 100× error in its own input,
and every downstream claim would inherit it. Fixed so percentage-united values render in points.
This was the most dangerous bug in the module — silent, plausible-looking and unrecoverable.

**2. The citation auditor split sentences on decimal points.** `33,543.00` became two sentences,
orphaning the citation that followed and reporting 50% coverage on a perfectly cited answer —
a false failure that would have trained users to ignore the audit. Fixed with a decimal-aware
splitter.

**3. My abstraction test was too blunt.** It grepped for vendor *names* and failed on docstrings
that legitimately explain which providers share a dialect. Rewritten to parse the AST, strip
docstrings, and assert on the two things that actually matter: no hard-coded endpoints, no
branching on provider name.

**4. My retry test recursed infinitely.** `monkeypatch.setattr(asyncio, "sleep", lambda: asyncio.sleep(0))`
patched the function it then called. Fixed by patching the router module's reference with a
genuine coroutine.

---

## 11. On the offline provider

No live API key exists in this environment. Rather than ship an untestable layer, I built a
deterministic offline analyst that composes prose **from the grounded evidence block**, obeying
the same citation and hedging rules.

This is not a stub returning placeholder text. It means grounding, citation verification,
guardrail classification, memory, caching and cost accounting are all exercised for real — only
the language model is simulated. A test asserts it is *structurally incapable* of emitting a
number that is not in the evidence.

When a key is configured the router prefers the live provider automatically. The offline
provider ranks last by construction and becomes dormant. The UI states this plainly rather than
implying live AI.

---

## 12. Known limitations

1. **No live LLM has been exercised.** Transport, payloads, auth headers and response parsing are
   unit-tested against all three wire formats, but no real API round-trip has occurred. This is
   the honest gap: the plumbing is verified, the vendor behaviour is not.
2. **Streaming degrades to chunked completion** for providers without a true SSE adapter. The
   interface is correct; per-provider SSE parsing is a later refinement.
3. **Document evidence is empty.** `EvidenceKind.DOCUMENT` and `document_search` exist and are
   wired, but nothing populates them until Module 7's ingestion pipeline lands. The conference-call
   and annual-report capabilities correctly say so rather than inventing content.
4. **Memory is in-process.** Sessions do not survive a restart or span workers. Swapping the
   store for Redis is a one-class change; it was not warranted before Module 10's deployment work.
5. **Claim classification is heuristic.** It is a reading aid, not a semantic guarantee — right
   far more often than wrong, and it degrades toward caution.
6. **Tools are read-only projections** of the grounded context rather than true function-calling.
   Native tool-calling per provider is a natural extension once a live key exists.
