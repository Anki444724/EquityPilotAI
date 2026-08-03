# Multilingual AI Response Engine — deployment validation report

**Status:** deployed, backend SHA `1f2c9e7`, frontend SHA `62f3607`
**Validation:** 29/29 production checks passed against the live database
**Tests:** 2,778 passing, 0 failures (166 new)
**Live:** https://backend-production-18956.up.railway.app

---

## 1. What was built

Automatic language detection and response rendering for English, Hindi
(Devanagari) and Hinglish, with an architecture that makes a seventh language a
translation module rather than a project.

```
User
  ↓
Retriever → RAG → Knowledge Graph → Scoring → Reasoning     ← all UNCHANGED
  ↓
Language Adapter
  ↓
English · Hindi · Hinglish · (Marathi, Gujarati, Tamil, Telugu, Kannada, Bengali)
```

The adapter is the **only** component that knows a second language exists. It
operates at both ends of the pipeline and nothing in between changed:

| Direction | What happens | Effect |
|---|---|---|
| **Inbound** | The question is rewritten into English retrieval terms | `Revenue`, `राजस्व`, `kamai` and `earnings` all hit the same English index |
| **Outbound** | The finished English answer is rendered into the user's language | Only explanatory prose changes |

---

## 2. Pre-implementation audit

The audit is recorded in `docs/MULTILINGUAL_AUDIT.md`. Four findings shaped the
design:

1. **`ResearchAnalyst._finalise()` is a single funnel.** Every capability,
   chat turn, report section and section-writer call passes through it. One
   insertion point therefore reaches 100% of generated prose — no
   per-capability work and no report-orchestrator changes.

2. **Retrieval was already script-tolerant but not cross-lingual.** The
   tokeniser is `[\w\u0900-\u097F]` and the Postgres config is `simple`, not
   `english`, so Devanagari tokenises correctly today. What fails is
   *matching*: the corpus is English, so `राजस्व` shares no token with
   "revenue". That is solvable by rewriting the query, above retrieval —
   which is why no retrieval code changed.

3. **Citation markers are structural.** `annotate()` rewrites `[revenue]`
   against the citation index. A translator touching those brackets breaks the
   citation audit, the guardrail check and the evidence chain at once. This
   became the highest-risk component and drove the protection design.

4. **`User.preferences` (JSON) already exists.** "Remember the preference"
   therefore needed **no migration**, satisfying the brief's constraint
   outright.

---

## 3. Language detection

Detection has to be right on very short input — "Revenue kya hai" is fourteen
characters — which rules out n-gram identifiers trained on prose. The
implementation scores three signals with a deliberate asymmetry:

| Signal | Weight | Why |
|---|---|---|
| Devanagari script | decisive, short-circuits | Nobody types Devanagari by accident |
| Romanised Hindi **function words** | 2.0–3.0 | Grammar is the give-away, not vocabulary |
| English function words | 0.6 | Weak — they appear throughout Hinglish too |

The asymmetry is the crux. "Revenue growth kaisa hai" is two English tokens and
two Hindi ones; a majority vote returns English and the feature fails on the
brief's own examples. Words with English homographs (`he`, `to`, `so`, `me`,
`is`, `do`, `in`) are **excluded** from the Hindi list — including them made
"How much is the revenue" score as Hinglish, which is the worst possible
misdetection.

**Result: 15/15 on the brief's examples, verified live.**

| Question | Detected | Confidence |
|---|---|---|
| How is TCS? | english | 0.76 |
| टीसीएस कैसी कंपनी है? | hindi | 0.99 |
| TCS kaisi company hai? | hinglish | 0.82 |
| Revenue kya hai | hinglish | 0.82 |
| Revenue क्या है | hindi | 0.91 |
| How much revenue | english | 0.76 |

Detection is deterministic — a session that answered in Hindi then switched to
English on a follow-up would look broken — and every result carries a `reason`
string so a misdetection is diagnosable from a log line.

### Precedence

1. **Explicit request** — `language=hindi` is an instruction.
2. **Confident detection** — typing Devanagari beats a preference saved last week.
3. **Saved preference** — breaks ties when detection is a guess (`TCS revenue`
   scores 0.35 and defers).
4. **Detection anyway.**

---

## 4. One canonical knowledge base

The brief's central prohibition, enforced structurally and asserted by tests
that fail the build:

| Guarantee | How it is enforced | Live result |
|---|---|---|
| No per-language table | Metadata scan for `translat`/`hindi`/`locale` | 65 tables, 0 found |
| No per-language column | Column scan on all six content tables | 0 found |
| No duplicated chunk | Group by (document, page, chunk_index) | 0 duplicates |
| Language layer writes nothing | Source scan for `db.add`/`commit`/`session` | 0 occurrences |
| Language layer imports no ORM model | Source scan for `from app.models` | 0 occurrences |
| Translators are stateless | Instance `__dict__` must be empty | verified |

Cross-language search is a **query-side dictionary of ~200 entries**, not a
translated index. Translating 11,485 chunks would create the second knowledge
base the brief forbids, require a full re-embed, and double every future
ingestion. The rewrite changes nothing downstream and is exactly reversible.

---

## 5. Protection of untranslatable content

The brief lists what must never be translated. The mechanism is **mask →
translate → restore → independently verify**.

Instructing a model not to touch certain things is not a control: models
transliterate "TCS" to "टीसीएस" and convert "₹2,55,324 crore" to "255 billion
rupees" with no ill intent, and both destroy the citation audit.

Sentinel design, by elimination:

| Candidate | Rejected because |
|---|---|
| `{0}` | Models reformat braces and renumber them |
| `[[0]]` | Collides with the citation markers being protected |
| `<x0>` | Models close, nest and pretty-print anything XML-like |
| `\ue000` private use | Invisible — a dropped one leaves no trace |
| **`§0§`** | **Chosen.** Printable, absent from financial prose, not markup, visible in a diff |

Protected categories: citation markers, markdown links, ISINs, fiscal years
(`FY2025`, `Q1FY26`, `2024-25`), all numbers with Indian or Western grouping,
tickers, and caller-supplied company names — the one category no pattern can
recognise, since "Asian Paints" looks exactly like ordinary prose.

**Verification is independent of the masking.** `verify_preserved()` re-extracts
protected spans from both strings and compares them, rather than trusting the
sentinel bookkeeping — a bug in `protect` would be invisible to a check built
on the same machinery.

**The engine fails closed.** A translation that loses a protected token is
**discarded** and the English original returned. English with an intact
evidence chain is strictly better than Hindi without one. This fired in
production on the very first live Hindi request and behaved exactly as designed
(see §8).

---

## 6. Financial glossary

121 terms, all 12 from the brief verbatim:

| English | Hindi | | English | Hindi |
|---|---|---|---|---|
| Revenue | राजस्व | | Free Cash Flow | फ्री कैश फ्लो |
| Net Profit | शुद्ध लाभ | | ROE | आरओई |
| Operating Margin | ऑपरेटिंग मार्जिन | | ROCE | आरओसीई |
| Debt | ऋण | | EPS | ईपीएस |
| Cash Flow | कैश फ्लो | | PE Ratio | पीई अनुपात |
| Market Cap | मार्केट कैप | | Dividend | लाभांश |

Two rules govern it:

**Hindi keeps Devanagari where a term is genuinely current** (राजस्व, शुद्ध लाभ)
but transliterates where modern financial Hindi does (EBITDA → ईबीआईटीडीए).
Coining a Sanskrit compound produces prose no Hindi speaker recognises.

**Hinglish keeps the English term.** All 121 render unchanged. An analyst says
"operating margin achhi hai", never "parichalan labh maargin achhi hai" — a
Hinglish glossary that translated technical vocabulary would defeat the point.

The glossary is **advisory to the model, not find-and-replace**: Hindi inflects
for case and gender, so blind substitution produces ungrammatical output. Terms
are supplied as a reference table, filtered to those actually present in the
text.

---

## 7. Consistency across languages

Identical scores, citations and evidence are guaranteed **structurally**, not
by testing:

- `compute()` and `HybridRetrievalEngine.retrieve()` have **no `language`
  parameter** — asserted by test.
- `AIScoreResult` carries no language field.
- `audit()`, `check()` and `enforce()` all run on the **English** text; a test
  asserts `citation_audit = audit(...)` precedes `LanguageAdapter().adapt(...)`
  inside `_finalise`. Translation cannot alter what was verified.
- `content` (the audited, persisted artefact) stays English; only
  `display_content` is rendered.
- Conversation memory stores English, so switching language mid-session carries
  no untranslatable history.

End-to-end tests assert that citation markers, numbers and identifiers are
**byte-identical** across English, Hindi and Hinglish renderings of the same
answer.

---

## 8. Defects found and fixed

### PROTECT-001 — the masker corrupted its own output

The first implementation ran each protection rule over the whole working
string. The `number` rule then matched the digits *inside* an already-issued
sentinel: `§0§` became `§§3§§`, where span 3 was the digit "0". Restoration
produced garbage on any text containing both a ticker and a figure — which is
every sentence this platform generates.

```
TCS revenue was 100 crore in FY2025 [revenue].
→ §§3§§ revenue was§4§ore in §§5§§ §§6§§.        # corrupted
```

**Fix:** tokenise instead of substituting. The text is held as alternating
protected and unprotected fragments; each rule is applied only to fragments
still unprotected, so a sentinel once issued is inert. All round trips now
byte-exact.

### NORM-001 — a bare Hindi word was never normalised

The query rewrite was gated on detected language. A bare content word —
`kamai`, `munafa`, `karz` — carries no grammar, so detection returns English at
0.35 confidence and the rewrite was skipped entirely. The brief names `kamai`
explicitly as a query that must retrieve the same documents as `revenue`.

**Fix:** gate on the presence of a mappable term instead. Detection still
decides the *response* language; term presence decides whether the *query*
needs rewriting — genuinely different questions. The 60 romanised keys were
checked for collisions against common and financial English: there are none.

### TRANS-001 — a doomed round trip to the offline composer

Found by testing the live deployment. With the LLM quota exhausted the router
falls through to the **offline provider**, a template composer rather than a
model. Handed masked text it returns unrelated boilerplate, so all 30 sentinels
vanished, the integrity check correctly rejected the result, and the user
received pure English after paying for a call that could never have succeeded.

The fail-closed guarantee worked perfectly — but the round trip was pointless.

**Fix, in two parts.** The first attempt checked the *configured* provider
chain up front — and shipping it revealed it was insufficient. Production has a
live provider **configured but exhausted**, so the chain looks healthy and the
router falls through to the offline composer at request time. Re-testing the
live deployment showed the guard never firing and the answer still coming back
in pure English.

The reliable signal is which provider **actually served** the response, which
is only knowable afterwards. Both checks now run: the chain check avoids the
call entirely where possible, and a runtime check on `response.provider`
abandons after the first chunk otherwise. Either way the glossary translator
takes over, yielding partially-rendered Hindi with every citation and figure
intact, honestly labelled as terminology substitution rather than translation:

```
राजस्व of 2,55,324 crore [revenue] grew. ऋण fell 10.2%.
```

The check is deliberately conservative: any doubt, including a router whose
internals change, resolves to "try the model anyway", because wrongly skipping
a live provider would silently downgrade every translation.

### Harness bugs — mine, not the product's

- **`asyncio.get_event_loop()`** in the test helper. Passed when the file ran
  alone; failed all 13 async tests in a full-suite run, because Python 3.13 no
  longer creates a loop implicitly and an earlier module had closed the
  existing one. Replaced with `asyncio.run`.
- **A code-inspection test split on the first `if memory is not None:`** —
  which is in `_refuse`, a path with no model response at all. The assertion
  was reading the wrong function. Scoped to `_finalise`.
- **Two validation checks were wrong, not the product** (see §9).

---

## 9. Two validation checks that were wrong

Reported because a harness that fails for the wrong reason is worse than no
harness.

**"The corpus contains no Devanagari chunks" — failed on 49 of 11,485.**
Inspection showed these are bilingual **source filings** that Indian issuers
publish: Bank of Baroda's registered-office block, an IOCL statement header.
Every one was ingested on 1–2 August, before the language layer existed. They
are original documents, not translations. The check was wrong — "no Devanagari
anywhere" would forbid the platform from ingesting genuine Indian regulatory
filings. Replaced with the check the brief actually implies: **no chunk is
duplicated per language**, which passes at 0.

**"No document is stored twice" — failed on one hash.**
`IRFC_Cash_Bank_Balance_FY2025_26.csv` is uploaded against two different
companies. That is a data-entry duplicate from the ingestion backlog, unrelated
to language and pre-dating this work. Now reported as an observation rather
than scored as a multilingual failure — failing this phase for it would
attribute someone else's defect to this change.

---

## 10. API

| Endpoint | Purpose |
|---|---|
| `GET /ai/languages` | Registry, including planned languages and glossary stats |
| `POST /ai/languages/detect` | Detection **plus** the English query the retriever receives |
| `PUT /auth/me/language` | Remember a preference (existing JSON column, no migration) |
| `GET /auth/me` | Now returns the saved `language` |

`language` is accepted on `/ai/chat`, `/ai/analyse` and `/ai/report`, defaulting
to `auto`.

`/ai/languages/detect` exists because cross-language search is the least
visible part of the feature: a user whose Hindi question returns nothing needs
to see the rewrite before anyone can tell whether detection, normalisation or
the corpus was at fault.

### Backward compatibility — verified live

| Check | Result |
|---|---|
| `language` defaults to `auto` on every request model | ✓ |
| Language block **absent** from English responses | ✓ verified on production |
| A client that never sends `language` | ✓ HTTP 200, payload unchanged |
| `ResearchAnalyst.run(language=None)` default | ✓ English path untouched |
| Invalid language | ✓ HTTP 422, not a silent fallback |

---

## 11. Future languages

All six are **already declared** and published by `GET /ai/languages` with
`status: planned` — the roadmap is inspectable rather than asserted:

```json
"supported": ["english", "hindi", "hinglish"],
"planned":   ["marathi", "gujarati", "tamil", "telugu", "kannada", "bengali"]
```

Enabling one requires: a `LanguageSpec` status flag, and a translator that
supports it. `LLMTranslator.supports()` returns `True` for any language by
construction — the model does the work, the platform supplies the instruction
and the glossary. **No retrieval, scoring, RAG or database change; no
migration.** A test asserts that retrieval, scoring and knowledge packages never
import the language layer, so the boundary cannot erode silently.

---

## 12. Frontend

- `LanguageSelector` — Auto by default (a selector defaulting to a named
  language would defeat the point), supported languages, and planned ones shown
  greyed as "coming soon".
- Preference persists to the backend **and** mirrors to `localStorage`, so the
  control renders correctly on first paint instead of flickering.
- Chat bubbles carry `lang={bcp47}`, so Devanagari renders with the correct
  font and screen readers switch voice. Without it, Devanagari falls back to
  whatever glyphs the Latin font happens to carry.
- When a response comes back in English despite a Hindi request, the reason is
  shown. Silently serving English is the failure most likely to erode trust.
- Typechecks clean; `next build` succeeds; all 17 routes build.

---

## 13. Caveats and known limitations

- **Translation quality depends on the LLM, and the free tier is exhausted.**
  Production currently falls through to the glossary translator, which renders
  financial terms but leaves surrounding prose in English. It is labelled as
  partial. Full fluent Hindi requires OpenRouter credit (~$10 for 1,000
  req/day).
- **Hindi *retrieval* is still weak, and this feature does not fix it.** Query
  normalisation makes Hindi queries hit the English index via the term
  dictionary — a real improvement over the measured 0.0026 MRR — but a Hindi
  question using vocabulary outside the ~200-entry dictionary still degrades.
  Genuine semantic Hindi retrieval needs the embeddings backfill.
- **The query dictionary is hand-built.** It covers the financial vocabulary a
  user is likely to type, not the whole language.
- **Devanagari script does not distinguish Hindi from Marathi.** Devanagari
  resolves to Hindi and records `ambiguous_with: [marathi]` rather than
  guessing. A Marathi module would resolve it.
- **Detection is heuristic, not a model.** Deterministic and fast, but a
  Hinglish question using no function words ("TCS ka margin") may score as
  English at low confidence — which is why a saved preference breaks that tie.
- **Transliteration is not attempted.** A Hindi speaker typing a company name
  in Devanagari (`टीसीएस`) will not match the ticker `TCS` in retrieval.
- **The offline-only guard reads router internals defensively.** If the router
  changes shape the guard degrades to "try the model anyway", which is the safe
  direction but means TRANS-001 could silently return.
- **Only chat, analyse and report accept `language`.** The streaming endpoint
  does not yet — streaming translated text requires buffering the whole
  response, which defeats streaming.
- **The repository is still public.**
