# Part 2D — Open-Ended Internal AI

**Status:** implemented, wired behind the existing Part 2C opt-in.
**Module:** `backend/app/services/ai/internal_open_ended.py`
**Tests:** `backend/tests/test_internal_open_ended.py` (behaviour),
`backend/tests/test_internal_open_ended_architecture.py` (invariants)
**Baseline:** `551450ceb1015b3ea043bbf57e7c857d33d3522f` (Part 2C)

---

## 1. Objective

Part 2B wrote an execution route down and left it empty:

```python
#: Needs internal reasoning that does not exist yet.
INTERNAL_REASONING = "internal_reasoning"
```

Part 2D is that reasoning. It answers the questions the deterministic
engines are *structurally* unable to serve — a comparison between two
figures, or a genuinely open-ended question — from evidence the platform
already holds, with no provider in the path.

It is **not** a language model and does not try to be one. There is no
prompt, no provider, no retrieval, no neural component and no interpreter.
The capability set is closed, the arithmetic set is closed, and every
vocabulary is an explicit table in one file. A question the layer cannot
answer from existing evidence returns `NOT_SUPPORTED`, and the existing
retrieval/provider path answers it exactly as it did before Part 2D
existed.

The design goal is a self-owned AI capability that is:

| property | how it is achieved |
|---|---|
| deterministic | same question + same context ⇒ identical `InternalAnswer`; tested |
| bounded | closed capability enum, closed operation enum, `MAX_OPERATIONS = 6` |
| evidence-grounded | every figure is a context citation or arithmetic on two of them |
| auditable | derived figures are published as `Citation`s the existing audit resolves |
| fail-closed | every unsafe condition returns a refusal carrying no text |
| extensible | add a table row and a test; no matcher is ever generalised |

---

## 2. Architecture

```
question
  → FinancialIntentResolver        (Part 1/2A — unchanged, answers first)
  → QuestionPlanner.plan()         (Part 2B — unchanged, planned ONCE)
  → InternalComposer.compose_answer()   (Part 2C — COMPOSITION_REQUIRED)
  → InternalOpenEndedEngine.answer()    (Part 2D — INTERNAL_REASONING)
  → _deterministic() → _verify_and_record()   (the one existing funnel)
  → retrieval + provider           (existing fallback — unchanged)
```

`InternalOpenEndedEngine` is stateless, constructed once per analyst in
`AIService.analyst_for`, and injected into `ResearchAnalyst` alongside the
planner and composer.

### Contracts

All `@dataclass(frozen=True, slots=True)`:

| type | role |
|---|---|
| `OpenEndedCapability` | what kind of question is being answered (7 members) |
| `InternalAnswerStatus` | `ANSWERED` or `NOT_SUPPORTED` — no third state |
| `OperationKind` | the closed arithmetic set (4 members) |
| `InternalEvidenceRequirement` | metadata about needed evidence; never a value |
| `InternalOperation` | one executed step: inputs, output key, result, reason |
| `InternalAnswerPlan` | the plan for an answer, loggable independently of prose |
| `InternalAnswer` | the answer or the refusal, with its citations and steps |

`InternalEvidenceRequirement` deliberately carries **no value**, for the
same reason the planner's `EvidenceRequirement` carries none: a requirement
that already held a number would be an answer, and every safety property
here rests on the answer containing nothing the platform did not compute.

---

## 3. Routing

Part 2D owns **exactly one** execution route: `INTERNAL_REASONING`.

The planner reaches that route only for `QueryType.COMPARISON` or
`QueryType.OPEN_ENDED` — two subjects placed against each other, or a
question with no recognised intent and no financial vocabulary. Every other
route keeps its existing owner:

| route | owner | Part 2D behaviour |
|---|---|---|
| `DETERMINISTIC_FINANCIAL` | `FinancialAnswerEngine` (Part 1) | refuses |
| `DETERMINISTIC_INVESTMENT` | `InvestmentAnswerEngine` (Part 2A) | refuses |
| `COMPOSITION_REQUIRED` | `InternalComposer` (Part 2C) | refuses |
| `SOURCE_ROUTER` | existing source routing | refuses |
| `DECLINE` | existing provider fallback | refuses |
| **`INTERNAL_REASONING`** | **Part 2D** | answers or refuses |

**No question an existing route already answers can be intercepted**,
because of ordering in `ResearchAnalyst.ask()`:

1. `FinancialIntentResolver().resolve()` runs first. A single supported
   intent is answered there and the internal layers are never consulted.
2. `InternalComposer` is offered the plan next. A multi-intent question is
   joined there.
3. Only then is `InternalOpenEndedEngine` offered the same plan.

The route is checked twice — at the analyst call site and again inside the
engine — so the ownership rule is visible where the call is made *and*
enforced where the answer is built.

### One plan per question

Planning is performed once in `ResearchAnalyst._plan()` and the resulting
`QuestionPlan` is handed to both internal layers. Planning twice would cost
a company-resolution pass and a language-normalisation pass for nothing,
and two plans for one question could disagree about what was asked.
Asserted by `test_one_plan_serves_both_internal_layers`.

---

## 4. Capability taxonomy

| capability | status | what it does |
|---|---|---|
| `FACT_LOOKUP` | executed | one non-numeric company attribute from authoritative context |
| `CALCULATION` | taxonomy | derived figures, produced by the operation set below |
| `COMPARISON` | executed | two metrics of the same company, stated with measurements |
| `EXPLANATION` | executed | a definition from the bounded internal vocabulary |
| `MULTI_STEP_ANALYSIS` | executed | several bounded operations, in order, capped |
| `UNSUPPORTED_OPEN_ENDED` | refusal | recognised as open-ended, not answerable internally |
| `AMBIGUOUS` | refusal | subject or metric not pinned to exactly one thing |

`CALCULATION` names a capability; the arithmetic lives in
`OperationKind`, because a capability with no operation behind it would be
decoration.

### Capability selection

Selection is driven by the **question's shape**, never by which evidence
happens to be present — selecting by available data would answer a
different question than was asked.

```
wants_comparison and ≥2 metrics  → COMPARISON / MULTI_STEP_ANALYSIS
wants_comparison and  1 metric   → AMBIGUOUS refusal (the other side is not guessed)
definition ask and known term    → EXPLANATION
attribute ask and known term     → FACT_LOOKUP
known term, no definition ask    → EXPLANATION
metrics named, no comparison ask → UNSUPPORTED_OPEN_ENDED refusal
nothing recognised               → UNSUPPORTED_OPEN_ENDED refusal
```

---

## 5. Evidence model

Reused, not duplicated:

- `GroundedContext` — the context the analyst already built. Part 2D never
  builds, widens or refreshes one; it imports `ContextBuilder` for the type
  only. No second `ContextBuilder` execution, no second company resolution,
  no second scoring pass, no second database query.
- `Citation` and `EvidenceKind` from `app.domain.ai.types`.
- `safe_div` from `app.domain.calc` — where the platform's
  undefined-ratio-returns-`None` rule is defined, once.

The `_find` rule matches `FinancialAnswerEngine._find` exactly: a citation
whose value is `None` is an **unavailable figure, not a zero**, and never a
blank to be filled.

### Derived figures

A figure this layer computes — a difference, a multiple — is not in the
context. Left alone, the existing citation audit would correctly report it
as a number the evidence does not contain. So each derived figure is
published back as a `Citation` in the **same** architecture:

```python
Citation(
    key="derived_difference_revenue_pat",
    label="Difference between Revenue and Profit after tax",
    kind=EvidenceKind.RATIO,
    value=30092.2,
    unit="₹ cr",
    source="internal arithmetic on platform figures",
    fiscal_year=2025,
)
```

This is precisely what `ContextBuilder._add_ratios` already does for
computed ratios — it is an extension of an existing practice, not a second
citation model. The keys are prefixed `derived_`, the source string names
the arithmetic, and no new `EvidenceKind` was added.

`ResearchAnalyst._deterministic()` gained one keyword-only parameter,
`extra_citations: tuple[Citation, ...] = ()`, defaulting to empty. The
Part 1 and Part 2C paths pass nothing and are unchanged.

### Nothing is manufactured

- Every `used_citations` entry came from the context (asserted).
- Every `derived_citations` entry is `derived_*` with an arithmetic source
  (asserted).
- Citation keys are de-duplicated, one key once (asserted).
- No URL, document id or page reference is ever invented — this layer
  creates no document evidence at all.

---

## 6. Deterministic operations

`OperationKind` is a closed enum. This is the security boundary: there is
no `eval`, no `exec`, no shell, and no callable taken from data. A `kind`
that is not a member cannot be constructed, so data cannot smuggle
behaviour into the executor.

| kind | arithmetic | undefined when |
|---|---|---|
| `LOOKUP` | none — reads one citation | the citation is absent or `None` |
| `DIFFERENCE` | `a - b` | either input missing |
| `PERCENTAGE_CHANGE` | `(a - b) / \|b\|` | `b` is zero |
| `RATIO` | `a / b` (via `safe_div`) | `b` is zero |

Rules enforced in code:

- **explicit inputs only** — an operation names its citation keys; nothing
  is inferred.
- **no invented inputs** — a missing input yields `result = None`, never a
  substitute.
- **units preserved** — a derived figure inherits its inputs' unit.
- **division-by-zero safe** — `safe_div` returns `None`; the operation
  records a reason and the figure is withheld rather than reported as `0`.
  A withheld figure is *not* the same fact as a genuine zero.
- **incompatible units are not subtracted** — a percentage and a money
  figure have no meaningful difference, so both measurements are stated and
  the arithmetic is explicitly withheld, with the reason in the answer.
- **reproducible and traceable** — every operation records its inputs, so
  any figure in an answer can be recomputed from the citations beside it.

---

## 7. Multi-step limits

`MAX_OPERATIONS = 6`. Multi-step reasoning is permitted; unbounded
reasoning is not. Operations are truncated to the cap before being
recorded, and a question needing more steps than that is better refused
than half-answered — a truncated analysis reads as a complete one.

Every step is retained in `InternalAnswer.operations`, including the steps
that failed, so a log line shows how far the layer got rather than only
that it stopped.

Worked example — *"Compare revenue and pat, and what is the sector?"*:

| # | kind | inputs | output |
|---|---|---|---|
| 1 | `LOOKUP` | `revenue` | `revenue` |
| 2 | `LOOKUP` | `pat` | `pat` |
| 3 | `DIFFERENCE` | `revenue`, `pat` | `derived_difference_revenue_pat` |
| 4 | `RATIO` | `revenue`, `pat` | `derived_ratio_revenue_pat` |
| 5 | `LOOKUP` | `company_sector` | `company_sector` |

---

## 8. Composition

Answer text is canonical English, assembled from templates over real
values, with `[key]` markers in exactly the form the existing citation
audit resolves. Number formatting reproduces the platform's two rules
(percentages stored as fractions but shown in percentage points; `x`
rendered as a multiple) so the audit can match every figure back to its
source.

Two properties are written into the prose rather than left to chance:

- **A comparison is a measurement, never a ranking.** No "better",
  "worse", "best", "winner" or "superior" is produced. `test_a_comparison_never_declares_a_winner`
  asserts this both by word check and by running the platform's own
  `guardrails.check()`.
- **No part of the question is silently dropped.** When a question asks for
  a comparison *and* an attribute, both are answered. If the attribute is
  unavailable, the answer says so and records it in `missing` — which keeps
  the answer whole without inventing the figure.

---

## 9. Verification

Internal answers go through the **one** existing funnel. There is no
second final-response pipeline.

```
InternalAnswer → _deterministic() → _verify_and_record()
                   ├─ audit(raw_content, context.citations + derived)
                   ├─ check(raw_content, audit)      guardrails
                   ├─ enforce(raw_content, report)   disclosure
                   ├─ memory.add(...)                canonical English
                   ├─ annotate(content, citations)   readable markers
                   └─ LanguageAdapter.adapt(...)     LAST
```

Verified for an internal answer (all asserted in tests):

| check | result |
|---|---|
| answer exists | `status is ANSWERED` and `content` non-empty |
| status valid | closed enum, two members |
| evidence requirements satisfied | refusal whenever a required input is absent |
| numerical claims supported | `citation_audit.uncited_numbers == []` |
| citations valid | `citation_audit.unknown_keys == []`, `is_supported is True` |
| no fabricated citations | derived keys are `derived_*` with an arithmetic source |
| calculations reproducible | each operation records its inputs |
| missing evidence preserved | `missing` reaches the answer and the log |
| company identity safe | `_company_identity_is_safe`, the Part 2C gate, reused |
| guardrails passed | `guardrails.passed is True` |
| memory preserved | canonical English turn stored, no disclosure footer |
| annotation preserved | `[revenue]` → `[Revenue]` in `display_content` |
| LanguageAdapter last | invoked after audit, check, enforce and memory |

`_deterministic()` is shared by all three provider-free shapes —
`DeterministicAnswer`, `ComposedAnswer`, `InternalAnswer` — widened by a
parameter rather than forked.

### Metadata

```
provider          = "deterministic"
model             = "none"
prompt_tokens     = 0
completion_tokens = 0
cost_usd          = 0.0
latency_ms        = measured
```

No LLM generated the response, and the ledger does not pretend otherwise.

---

## 10. Fallback

Fail-closed is total. A refusal carries **no text** — `content == ""` — so
there is no partial internal answer for the caller to inherit. Every one of
these returns `NOT_SUPPORTED` and the question goes to the existing
retrieval/provider path unchanged:

| condition | capability |
|---|---|
| unsupported question | `UNSUPPORTED_OPEN_ENDED` |
| missing required evidence | `FACT_LOOKUP` / `COMPARISON` |
| ambiguous entity (several companies named) | `AMBIGUOUS` |
| ambiguous metric ("debt" is gross *and* net) | `AMBIGUOUS` |
| conflicting evidence (one key, two values) | `COMPARISON` |
| unsupported operation | impossible to construct |
| incompatible units for the arithmetic | answer given, arithmetic withheld |
| route not `INTERNAL_REASONING` | `UNSUPPORTED_OPEN_ENDED` |
| internal exception | caught at the analyst boundary |

`ResearchAnalyst._open_ended()` wraps the engine call. A bug in Part 2D
costs the user a fallback, not their answer; the failure is logged in full
server-side and no internal detail or stack trace reaches the response.

**No company is ever chosen on the user's behalf.** A question naming two
companies is refused, not resolved by picking one, because the guess would
be invisible in the output.

---

## 11. Provider boundary

The internal path imports no provider, no HTTP client and no SDK. Enforced
four ways in `test_internal_open_ended_architecture.py`:

1. **Import scan** — the module's AST imports contain none of `openai`,
   `anthropic`, `google`, `openrouter`, `groq`, `mistralai`, `cohere`,
   `httpx`, `requests`, `aiohttp`, `urllib`, `socket`,
   `app.services.ai.providers`, `app.services.language.translators`.
2. **Token scan** — the module's *code* (docstrings stripped via AST, so
   this document cannot satisfy the check) never names a provider.
3. **Subprocess import** — importing the engine in a clean interpreter
   loads no `app.services.ai.providers` module.
4. **Subprocess execution** — *answering a question* in a clean interpreter
   loads no LLM provider module, and with `socket.socket`,
   `socket.create_connection` and `socket.getaddrinfo` patched to raise,
   the engine still answers. No network is required.

Verified directly: importing and running the engine loads **zero** LLM
provider modules. The eight `app.data.providers` modules that do load are
the finnhub / yahoo / fmp market-data clients, pulled in transitively by
`context_builder` — exactly as they are for Part 2C's `internal_composer`,
which imports the same module for the same type.

**Existing provider and RAG fallback remains fully intact.** No provider
was removed, no router was edited, no retrieval call was deleted.
`TestProviderFallbackRemainsAvailable` asserts every provider module still
exists, the analyst still imports `ProviderRouter`, and
`self._retrieve(` / `self.router.complete(` are still in the answer path.
Removing providers is **Part 2E**, a separate task.

---

## 12. Security boundary

| threat | control |
|---|---|
| arbitrary code execution | closed `OperationKind` enum; no `eval`/`exec`/`compile`/`__import__` |
| dynamic dispatch from data | no callable stored in any table; `InternalOperation` has no `Callable`/`Any` field |
| shell execution | no `subprocess`/`os`/`pty`/`shlex`/`ctypes` import |
| dynamic arbitrary imports | no `importlib`, no `__import__`, no `getattr`/`setattr` |
| hidden LLM substitute via web API | no networking import; socket patched to raise in a subprocess test |
| unbounded reasoning | `MAX_OPERATIONS = 6` |
| fabricated figures | citations or arithmetic on citations; nothing else can enter the text |
| fabricated citations | derived keys are `derived_*` and resolve through the existing audit |
| wrong-company answer | `_company_identity_is_safe`, the same gate Part 2C uses |
| partial answer escaping | a refusal carries no text |
| stack-trace leak | exception caught at the analyst boundary, logged server-side |
| duplicate architecture | no second citation, scoring, valuation, engine or language system |

The engine holds no state after construction and writes nothing: no
session, no cache, no database, no file.

---

## 13. Multilingual behaviour

Reasoning is canonical English. There is no Hindi or Hinglish reasoning
engine and no per-language answer. Rendering is the existing
`LanguageAdapter`'s job and runs last, so a translation can never change
what was audited or what memory stores.

Detection runs over the **raw question and the planner's normalised English
form joined at a sentence break** — the same both-passes approach the
planner uses for its own vocabulary. Neither alone is sufficient: Hinglish
keeps its English technical terms in the raw text, while a Devanagari
question only becomes recognisable after normalisation. They are joined
with a sentence break rather than a space so a pattern cannot match across
the boundary and invent a phrase that appears in neither.

Both word orders are matched for the attribute ask, because both arrive:
English puts the interrogative first (*"what is the sector"*), while Hindi
and Hinglish put it last and the adapter's inbound normalisation preserves
that order — `"सेक्टर क्या है?"` becomes `"सेक्टर what"`, not
`"what सेक्टर"`. Matching only the English order would make the same
question answerable in one language and not another. Tested in English,
Hindi and Hinglish; all three reach the same English answer.

Protected content (company name and ticker) is passed to the adapter as
`entities`, exactly as for every other answer. Entity protection itself is
the existing translator implementations' responsibility — they wrap the
text in `protect(text, extra_terms=entities)` — and was not reimplemented.

---

## 14. Planner integration

**The planner was not modified.** No classification, route, confidence,
vocabulary or false-positive protection changed. Part 2D simply provides
the layer `INTERNAL_REASONING` already named, so all Part 2B behaviour and
all 135 collected planner tests are untouched.

One architecture allowlist entry was added, deliberately and by name:
`TestWiredConsumers.PERMITTED` in
`tests/test_question_planner_architecture.py` now includes
`internal_open_ended.py`, with a reason recorded beside it. That class
exists so a new planner consumer is a decision rather than a line that
slips in.

The dependency is one-way and asserted: the engine reads a plan as data,
never constructs a planner, never calls `.plan()`, and no file under
`app/services/ai/planner/` mentions this module.

---

## 15. Financial intents

No `FinancialIntent` member was added, duplicated or intercepted. The eight
Part 2B investment intents are untouched. `FinancialIntentResolver` was not
modified. The existing engines remain authoritative for every intent they
own, and Part 2D can only ever see a question the resolver declined.

`METRIC_SPECS` maps surface language onto **existing citation keys**. It
contains no formula, so no formula is duplicated: WACC, DCF, CAGR, growth
and margins are all still computed in exactly one place.

---

## 16. Tests

### `tests/test_internal_open_ended.py` — 75 tests

| class | tests |
|---|---|
| `TestRouteOwnership` | 7 |
| `TestFactLookup` | 5 |
| `TestCalculation` | 6 |
| `TestComparison` | 6 |
| `TestExplanation` | 4 |
| `TestFailClosed` | 6 |
| `TestCitationIntegrity` | 5 |
| `TestProductionWiring` | 16 |
| `TestFailureHandling` | 3 |
| `TestVerificationPipeline` | 8 |
| `TestMultilingualRouting` | 3 |
| `TestApiSurface` | 6 |
| **total** | **75** |

### `tests/test_internal_open_ended_architecture.py` — 73 tests

| class | tests | property |
|---|---|---|
| `TestNoExternalProviders` | 15 | no LLM in the internal path (import scan, token scan, subprocess import, subprocess run) |
| `TestNoNetworkDependency` | 4 | no I/O; answers with sockets disabled |
| `TestNoArbitraryCodeExecution` | 23 | no `eval`/`exec`/shell/dynamic import/callable-in-data |
| `TestNoDuplicateArchitectures` | 9 | one citation, scoring, engine and language system |
| `TestPlannerConsumersRemainExplicit` | 4 | allowlist by name; one-way dependency |
| `TestProviderFallbackRemainsAvailable` | 5 | Part 2E not started; every provider still present |
| `TestVerificationFunnelRemainsShared` | 6 | one funnel, guardrails not bypassed, adapter last |
| `TestCapabilityTaxonomyIsClosed` | 4 | every capability has an executor or is a refusal |
| `TestEngineIsStatelessAndReusable` | 3 | stateless; identical answers on repeat |
| **total** | **73** | |

Regression coverage is the existing suite: Part 1
(`test_deterministic_analyst_path.py`, `test_financial_answer_engine.py`),
Part 2A (`test_investment_answer_engine.py`, `test_internal_renderer.py`),
Part 2B (`test_question_planner.py`, `test_question_planner_architecture.py`),
Part 2C (`test_internal_composer.py`, `test_composition_wiring.py`),
multilingual (`test_multilingual.py`, `test_multilingual_chat_flow.py`), and
the API/Blogger suites. None was weakened.

### Results

Full backend suite, `pytest tests/`, SQLite:

```
4448 passed, 6 skipped
```

Baseline before this change was `4300 passed, 6 skipped`; the 148 new tests
account for the difference and no existing test changed status. CI runs the
same suite on SQLite and Postgres, plus the architectural-invariants job.

---

## 17. Limitations

Stated plainly, because a limitation that is not written down becomes a
surprise.

1. **Level-figure questions are not answered internally.** "What is the
   revenue?" and "What is the net profit?" are classified `UNSUPPORTED` and
   routed to `DECLINE` by the planner, so they never reach this layer and
   stay on the provider path. Routing them here would require changing a
   Part 2B classification and breaking
   `test_no_intent_is_invented`. The evidence *is* available (`revenue` and
   `pat` citations); the routing decision is what defers them. Asserted
   rather than assumed by
   `test_a_level_figure_question_never_reaches_this_layer`.
2. **`FACT_LOOKUP` covers one attribute.** `sector` is the only non-financial
   attribute the platform holds on every company. There is no order-book,
   promoter, peer or shareholding data to look up, and none is inferred.
3. **`EXPLANATION` is deliberately small.** Eight terms. A term outside the
   vocabulary returns `UNSUPPORTED_OPEN_ENDED` and the provider answers —
   the correct outcome for a definition this module does not vouch for.
   Growing it means adding a row and a test, never generalising a matcher.
4. **Comparison is single-company only.** Two metrics of one company. A
   cross-company comparison would need evidence for a company this context
   does not carry, so it is refused rather than half-built.
5. **No winner is ever declared.** By design. Comparison reports
   measurements; verdicts belong to the scoring engine, not to arithmetic.
6. **Ambiguous metric names are refused.** "Compare the ROE and the debt"
   is refused, because "debt" is both `gross_debt` and `net_debt` and
   choosing silently would be invisible in the output.
7. **`PERCENTAGE_CHANGE` is in the operation set but not yet reachable from
   a question shape.** It is present so the arithmetic is complete and
   tested; no current vocabulary routes to it, and it is not exposed as a
   capability on its own.

---

## 18. Part 2E handoff

Part 2D adds an internal capability. It removes nothing.

**What Part 2E will still have to do:**

- Remove or gate the external providers — `providers/gemini.py`,
  `openai.py`, `openrouter.py`, `claude.py` and the `ProviderRouter`
  configuration around them.
- Decide the fate of `providers/mock.py` and the offline composer that
  `report_orchestrator` and `section_writer` fall back to.
- Retire the RAG retrieval path in `ResearchAnalyst._retrieve()` and the
  document-passage citations it admits, or define what remains internal
  about them.
- Replace the `LanguageAdapter`'s external translation providers with the
  internal renderer for every language, not just the paths that already use
  it.
- Re-decide every question that still falls back. Today those include all
  level-figure questions, all `DECLINE` and `SOURCE_ROUTER` routes, every
  open-ended question outside the bounded vocabularies, and every case Part
  2D refuses. Part 2E needs either an internal answer for them or an
  explicit, visible refusal — a provider removal that simply leaves them
  unanswered would be a regression dressed as a milestone.

**What Part 2E can rely on:**

- A clean internal boundary with a single injected collaborator and no
  provider import, so the internal path needs no changes to survive a
  provider removal.
- One verification funnel, so removing providers cannot orphan the audit,
  guardrails, annotation, memory or language rendering.
- `TestProviderFallbackRemainsAvailable`, which will start failing the
  moment a provider is removed — a deliberate tripwire, to be updated as
  part of Part 2E rather than quietly deleted.
- A deterministic accounting convention (`provider="deterministic"`,
  `model="none"`, zero tokens, zero cost) already used by three answer
  shapes.

---

## 19. What this change did not touch

- No provider removed, added or reconfigured.
- No deployment. No production EC2, database, Docker volume or compose
  change.
- No migration. No schema change.
- No frontend change — the response schema is unchanged, so there was no
  API change for it to follow.
- No existing test weakened. One architecture allowlist gained one named
  entry with a recorded reason.
