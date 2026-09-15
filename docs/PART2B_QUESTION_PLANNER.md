# Part 2B — Internal Question Planner

**Status: shadow mode.** The planner is in the repository and fully tested. It is
not in the answer path. Nothing in production calls it yet.

---

## 1. Purpose

EquityPilotAI is being made independent of external AI providers. The target
pipeline:

```
User Question
  -> Internal Language Understanding      (Language Adapter, inbound)   [exists]
  -> Internal Question Planner                                          [Part 2B]
  -> Evidence / Data Requirements
  -> Existing Deterministic Engines / Internal Reasoning
  -> Canonical English Answer
  -> Audit + Guardrails
  -> Internal Language Renderer                                         [Part 2A]
  -> Display
```

The Question Planner converts a natural-language question into a structured
plan, deterministically, with no external provider anywhere in the path.

It performs exactly five things:

1. **understanding** — what language and script the question is in;
2. **intent identification** — which of the platform's existing intents it asks for;
3. **entity identification** — which company it is about;
4. **data / evidence requirements** — what answering it would need;
5. **execution planning** — which route execution would take.

It does **not** answer. A plan names intents and evidence; reasoning and
composition produce the prose.

---

## 2. What the planner is not

These were explicit constraints and they are enforced by tests, not by
convention (`tests/test_question_planner_architecture.py`):

| Not this | Enforced by |
|---|---|
| another calculation engine | no `ScoringService` / `ValuationService` / `ForecastService` / `RatioService` import; no division or multiplication in the package |
| another valuation engine | no `ValuationService`; no `dcf` / `wacc` computation |
| another RAG system | no `DocumentService`; no search or retrieval call |
| another database | no `app.db` / `app.models` import; no `session.add` / `commit` |
| a second `CompanyResolver` | resolution is injected; the planner only applies the caller's rule to the result |
| a second citation generator | no `Citation(...)` construction anywhere in the package |
| an external-provider client | no `openai` / `openrouter` / `gemini` / `ProviderRouter` / `LLMTranslator` / HTTP client import; a subprocess test proves importing the planner loads no provider module |
| reasoning | no prose field exists on `QuestionPlan` |

---

## 3. Where it sits

```
backend/app/services/ai/planner/
├── __init__.py            public surface
├── types.py               QueryType, ExecutionRoute, Confidence, EntityStatus,
│                          IntentMatch, EvidenceRequirement, EntityResolution,
│                          QuestionPlan
├── vocabulary.py          IntentSpec — canonical patterns + phrases + aliases
│                          + negative evidence + precedence
├── intent_matcher.py      IntentMatcher — ordered, multi-intent
├── evidence.py            intent -> evidence requirements
└── question_planner.py    QuestionPlanner.plan()
```

The package sits beside the deterministic engines (`financial_intent.py`,
`financial_answer_engine.py`, `investment_answer_engine.py`) at the same layer.
It is a sibling of those engines, not a replacement for any of them.

---

## 4. `QuestionPlan`

```python
plan = QuestionPlanner(company_resolver=svc.named_in).plan(question)
```

| Field | Meaning |
|---|---|
| `original_question` | exactly as typed |
| `normalized_question` | the planner's English working text (planning only) |
| `language` / `detection` | from the Language Adapter's inbound detection |
| `entity` | `EntityResolution` — status, id/ticker/name, candidates, basis |
| `intents` | ordered `IntentMatch` — intent, matched rule, position, family |
| `query_type` | see §7 |
| `execution_route` | see §8 |
| `required_evidence` | `EvidenceRequirement` — metadata only, never a value |
| `confidence` | `HIGH` / `MEDIUM` / `LOW` / `NONE` |
| `ambiguity` | why the plan is less certain than it might be |
| `missing_requirements` | what would have to be supplied to execute safely |
| `source_directive` | the parsed source restriction, when there is one |
| `notes` | observations; never answer content |

Everything is frozen and serialisable (`plan.as_dict()`). There is no field
that could hold a sentence of prose, a figure, a score or a citation — the
type is the guardrail, not a convention.

---

## 5. Intent matching

### One vocabulary, not two

The canonical patterns live in `app/services/ai/financial_intent.py` and are
exposed as `INTENT_PATTERNS` — a read-only view over the same mapping the
resolver matches on. The planner reads them; it does not re-declare them. A
pattern is therefore defined once, and the planner cannot drift from the
engine that will execute its plan.

The one change to `financial_intent.py` is additive: two read-only aliases
(`INTENT_PATTERNS`, `SPECIFICITY_OVERRIDES`). `FinancialIntentResolver`,
`FinancialIntent`, `INVESTMENT_INTENTS` and `resolve()` are untouched, and
`tests/test_question_planner_architecture.py::TestExistingResolverUnchanged`
pins the resolver's behaviour intent by intent.

### What the vocabulary adds

* **phrases / aliases** — wording the canonical regexes miss, including
  Hindi and Hinglish. `"financially strong"` is plainly about financial
  quality, but no canonical pattern matched it.
* **negative evidence** — see below.
* **precedence** — which intent wins an overlap.

### Matching stages

1. **Negative evidence** — tested against the **raw** question. Normalisation
   rewrites `"pe kya"` into `"pe what"`, so the Hindi postposition is only
   visible before the rewrite.
2. **Earliest match per intent** — one intent yields at most one match, so an
   intent repeated three times is still one thing being asked for.
3. **Overlap resolution** — the longer, higher-precedence span absorbs the
   shorter span it contains.
4. **Specificity overrides** — the canonical `SPECIFICITY_OVERRIDES` table,
   applied as a second belt so the planner cannot disagree with the resolver.

Results are ordered by first occurrence: the order the user listed things in
is part of what they asked.

### Negative evidence — the cases that made this necessary

| Question | Naive result | Correct result |
|---|---|---|
| `"Reliance pe kya bolte ho?"` | `pe` | *(none)* — "pe" is the Hindi postposition |
| `"Does the company sell software?"` | `recommendation` | *(none)* — a product, not a SELL call |
| `"Company ka buyback hua?"` | `recommendation` | *(none)* — a corporate action |
| `"What are the promoter holdings?"` | `recommendation` | *(none)* — a shareholding fact |

Each guard is written so it cannot suppress the intent it protects:
`"Reliance ka P/E kitna hai?"`, `"P/E zyada hai kya?"`, `"Should I buy this
stock?"` and `"Should I hold or sell?"` all still resolve correctly.

### Overlap resolution

| Question | Naive result | Correct result |
|---|---|---|
| `"debt risk kya hai?"` | `debt` + `financial_risk` | `financial_risk` |
| `"What is the revenue growth?"` | `revenue_growth` + `growth_quality` | `revenue_growth` |
| `"Reliance financially strong hai?"` | `financial_quality` + `financial_risk` | `financial_quality` |

Asking for both genuinely still yields both:
`"Reliance ki financial health aur debt risk dono batao"` -> `financial_quality`,
`financial_risk`.

---

## 6. Entity resolution

There is no `CompanyResolver` class in this repository. The authoritative
mechanism is:

```python
CompanyService.named_in(text, *, exclude_id=None) -> list[Company]
```

The planner accepts it as an injected callable:

```python
QuestionPlanner(company_resolver=CompanyService(db).named_in)
```

Injection is what keeps the entity tests database-free, and what keeps the
point structurally: the planner never resolves a company itself and never
creates one.

| Resolver result | `EntityStatus` | Plan behaviour |
|---|---|---|
| exactly 1 | `RESOLVED` | usable |
| more than 1 | `AMBIGUOUS` | candidates recorded; the planner does not choose |
| 0, conversation has one pinned | `CONTEXT_ONLY` | usable, and flagged weaker than an explicit mention |
| 0, nothing pinned | `UNRESOLVED` | recorded in `missing_requirements` |
| resolver raised | `UNRESOLVED` | reported as a resolver failure, not as "nobody was named" |

`CONTEXT_ONLY` reads the existing `ConversationMemory` read-only. That is not
a second company-memory system — it is the same object the analyst already
uses.

---

## 7. Query types

| Type | Example | Meaning |
|---|---|---|
| `DETERMINISTIC` | `"Reliance ki financial quality kaisi hai?"` | one supported intent |
| `MULTI_INTENT` | `"...financial quality aur growth..."` | several supported intents |
| `COMPARISON` | `"Compare Reliance and TCS"` | two subjects placed against each other |
| `SOURCE_DIRECTED` | `"only uploaded documents me debt kya hai?"` | provenance restriction |
| `UNSUPPORTED` | `"Reliance ka profit kitna hai?"` | financial, but not yet supported |
| `OPEN_ENDED` | `"Tell me about Reliance"` | broad; nothing specific asked |
| `AMBIGUOUS` | `"Good company hai?"` | evaluative, but which intent is a guess |

The order of evaluation is how binding each condition is. A source
restriction is checked first because provenance outranks everything else:
answering from the database when the user asked for documents only is not a
partial answer, it is the wrong answer.

Source restriction reuses `app/domain/ai/sourcing.py::parse_directive()` —
the same function the analyst uses. It is not reimplemented. The existing
fail-closed source router remains authoritative; the planner only describes
the restriction.

---

## 8. Execution routes

The planner **names** a route. It never takes it.

| Route | When |
|---|---|
| `DETERMINISTIC_FINANCIAL` | one Phase 1 intent -> `FinancialAnswerEngine` |
| `DETERMINISTIC_INVESTMENT` | one Phase 2A intent -> `InvestmentAnswerEngine` |
| `COMPOSITION_REQUIRED` | several intents -> Part 2C |
| `SOURCE_ROUTER` | a source restriction is in force |
| `INTERNAL_REASONING` | comparison or open-ended; needs reasoning that does not exist yet |
| `DECLINE` | unsupported or ambiguous — declining *is* the answer |

Family membership is derived from `INVESTMENT_INTENTS`, not restated.

---

## 9. Evidence requirements

`planner/evidence.py` maps each intent to the evidence answering it needs,
using **only citation keys the ContextBuilder actually publishes**. The
mapping was taken by reading what the existing deterministic engines look up,
not by guessing.

Requirements carry metadata only — no value can be carried, because
`EvidenceRequirement` has no numeric field.

One honest gap is recorded rather than papered over:

> **P/B.** The ContextBuilder publishes no `pb` citation, and
> `FinancialAnswerEngine._build_pb` already reports P/B as unavailable rather
> than deriving it from price and book value. The planner marks the
> requirement `required=True, published=False` and lists it in
> `missing_requirements`.

---

## 10. Multilingual behaviour

The planner supports English, Hindi / Devanagari and Hinglish / Roman Hindi
by reusing `LanguageAdapter.normalise_query()` and the existing detection.

**Planning normalisation is for planner use only.** It is never handed back
to retrieval, scoring or the resolver — all three keep their existing inputs
unchanged. No language parameter was added to scoring or retrieval, and a
test asserts neither signature has one.

Equivalent questions produce equivalent plans. `"What is Reliance's financial
quality?"`, `"Reliance ki financial quality kaisi hai?"` and
`"रिलायंस की financial quality कैसी है?"` agree on intent, query type,
execution route and evidence requirements; they differ only in the reported
language and the original text.

Matching runs on the raw question first, because Hinglish keeps its English
technical terms intact and matching there preserves the exact order things
were asked in. The normalised form is the fallback, chiefly for pure
Devanagari, where the adapter's term mapping is what makes the vocabulary
visible.

---

## 11. Why `revenue` and `net_profit` are deferred to Part 2C

They are not intents in this repository. The ContextBuilder publishes
`revenue` and `pat` **citations**, but no `FinancialIntent` member consumes
them, and no `_build_revenue` / `_build_net_profit` exists.

Part 2B therefore does not:

* add them to `FinancialIntent`;
* modify `FinancialAnswerEngine`;
* create new answer builders;
* change any existing execution semantics.

A question like `"Reliance ka profit kitna hai?"` plans as `UNSUPPORTED` /
`DECLINE`, with an explicit note:

> `TODO — Part 2C: 'revenue' and 'net_profit' are not FinancialIntent
> members. The ContextBuilder publishes 'revenue' and 'pat' citations but no
> intent consumes them, so this is deferred rather than invented here.`

Inventing them here would have meant inventing execution behaviour in a phase
whose entire purpose is to describe execution.

---

## 12. Why multi-intent does not change the existing resolver

`FinancialIntentResolver.resolve()` returns `None` when more than one intent
matches. That is deliberate and it stays.

The rule protects the **execution** path. `FinancialAnswerEngine` and
`InvestmentAnswerEngine` answer exactly one intent each; handing either of
them one intent out of a two-intent question produces a partial answer, and a
partial answer is a wrong answer wearing the right clothes — every figure in
it is real, correctly cited, and nothing downstream flags the omission.

The planner asks a different question: not "may I execute exactly one of
these?" but "what is this question about?". Dropping the second intent would
defeat the exercise. So the two are separate objects over the same
vocabulary:

* `FinancialIntentResolver` — the execution gate; fail-closed; unchanged.
* `IntentMatcher` — planning only; returns every intent, in order.

A multi-intent question therefore routes to `COMPOSITION_REQUIRED`, which is
not executable today. That is the honest answer, and it is exactly the work
Part 2C exists to do.

---

## 13. Shadow-mode status and Part 2C integration

Confirmed by `TestShadowMode`:

* `analyst.py` does not import the planner; it still uses
  `FinancialIntentResolver`;
* no production module outside the planner package imports it;
* `ContextBuilder`, `FinancialAnswerEngine` and `InvestmentAnswerEngine` were
  not modified for it.

**Integration point for Part 2C.** `ResearchAnalyst.run()` currently does:

```python
intent = FinancialIntentResolver().resolve(retrieval_query)
if intent is not None:
    ... FinancialAnswerEngine / InvestmentAnswerEngine ...
```

The natural change is to plan first and branch on the plan:

* `DETERMINISTIC_FINANCIAL` / `DETERMINISTIC_INVESTMENT` -> the existing
  engines, unchanged;
* `MULTI_INTENT` -> the new composition layer, which runs each intent's
  engine and composes the parts;
* `SOURCE_DIRECTED` -> the existing source router, unchanged;
* `DECLINE` -> say so, without calling a provider.

Until that lands, a wrong plan costs nothing but a wrong plan.

---

## 14. Tests

| File | Covers |
|---|---|
| `tests/test_question_planner.py` | all 18 intents in English; Hindi; Hinglish; Roman Hindi; multi-intent; ordering; entity resolution; unresolved / ambiguous / context-only; unsupported; comparison; source-directed; open-ended; evidence mapping; routes; confidence; cross-language equivalence; the five false positives; `revenue`/`net_profit` deferral; no answer prose |
| `tests/test_question_planner_architecture.py` | no provider imports or instantiation; no `LLMTranslator`; no scoring / valuation / retrieval imports; no DB writes; no `Citation` construction; no embeddings; no financial arithmetic; no language parameter on scoring or retrieval; resolver behaviour unchanged intent by intent; Phase 1 and Phase 2A answer and citation-audit regression; shadow-mode confirmation |

Run:

```bash
cd backend
python -m pytest tests/test_question_planner.py \
                 tests/test_question_planner_architecture.py -q
```
