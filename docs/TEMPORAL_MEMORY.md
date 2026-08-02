# Temporal Memory — §8 and §12

Deployed as `9d33f8b`. One dated AI observation per company per fiscal year,
in the form you specified:

```
FY2025
  Management quality improving.
  Capex increasing.
  Debt reducing.
  ROCE improving.
  Confidence: 92%

FY2026
  Management delivered previous guidance.
  Moat strengthened.
  New plant commissioned.
  Confidence: 95%
```

## What makes it a track record

FY2026's line *"management delivered previous guidance"* is a judgement about
what FY2025 predicted. That only means anything if the platform actually
checked, so the generation loop is **chronological and stateful**: each year
receives the previous year's recorded guidance and is asked to rule on it,
citing this year's evidence.

Verified end to end on a controlled fixture:

| Year | Guidance recorded | Verdict | Reasoning |
|---|---|---|---|
| FY2025 | "commission Unit-3 during FY2026 and reduce net debt below 1.0x EBITDA" | `not_assessable` (no prior year) | — |
| FY2026 | — | **`delivered`** | "Unit-3 was commissioned ahead of schedule [E1] and net debt/EBITDA fell to 0.7x [E2], meeting both parts of FY2025 guidance." |

Credibility: **100% across 1 assessable year.**

Because the series is stateful, it cannot be generated in parallel. That is a
property of the design, not a performance oversight.

## Verdict discipline

A model will happily volunteer "delivered" for a company's first year, where
there is nothing to have delivered. Three rules are enforced **in code**
rather than left to the prompt:

1. **A verdict requires recorded prior guidance.** Without it the verdict is
   forced to `not_assessable`, whatever the model returns.
2. **Unassessable years leave the denominator**, rather than counting as
   average. `credibility_score` returns `None`, not `0.5` — a company nobody
   can grade is not an average company.
3. **Most Indian filings carry no numeric guidance**, so `not_assessable` is
   the expected common case, not a failure.

Live evidence of the discipline working: Cipla FY2027 was given FY2026
guidance about resolving USFDA observations at Goa. The FY2027 filings record
the inspection as classified VAI but never state remediation completed, and
the model returned `not_assessable` rather than claiming delivery.

## Anchoring and versioning

An observation is anchored to the fiscal year it is **about**, never to when
it was written. Ordering a narrative by ingestion time is how a late-arriving
2019 report ends up presented as current thinking.

Regenerating a year inserts a new version and supersedes the old one; nothing
is overwritten. This matters more here than elsewhere, because a year's
observation is the yardstick the *next* year was graded against — silently
rewriting it would change a judgement that has already been made. Cipla
FY2027 currently shows three versions (v1 0.20 superseded, v2 0.75 superseded,
v3 0.90 current), each retained with its evidence.

## Reading it back

| Endpoint | Purpose |
|---|---|
| `GET /company/{ticker}/observations` | The series, oldest first, with credibility |
| `GET /company/{ticker}/observations/{fy}/history` | Every version ever recorded for one year |
| `GET /company/{ticker}/credibility` | Delivery record from stored verdicts |
| `POST /company/{ticker}/observations/build` | Build or refresh, chronologically |

The timeline is also injected into the AI context ahead of raw chunks (§13),
as **one** citation for the whole series rather than one per year: the value
is the comparison across years, and ten fragments invite the model to quote a
single year and call it a trend. Confirmed live on Cipla — 1 temporal citation
among 77, at confidence 0.875. Fallback observations are excluded from the
prompt entirely, so template prose is never cited as analysis.

## Tracked dimensions

Eight fixed axes — `management_quality`, `capex`, `debt`, `roce`, `moat`,
`growth`, `margins`, `capital_allocation` — rather than free text, so a
ten-year series is comparable. "Management quality" in FY2018 must mean the
same axis as in FY2027 or the timeline is a pile of unrelated sentences.

Where a dimension has a measurable counterpart, the observation carries the
computed figure alongside the narrative trend and flags disagreement.
`contradicts_metric` is **reported, not corrected**: the filing may be
discussing a segment or a normalised figure, and silently overriding the model
would hide a real disagreement between narrative and accounts.

## OpenRouter

Usable again via **free-tier models**. `OPENROUTER_MODEL` was already
env-overridable by design, so this needed a Railway variable
(`nvidia/nemotron-3-super-120b-a12b:free`) rather than a code change.

I initially misread `limit_remaining: 19,999` as a restored balance. It is a
**rate limit, not a balance** — paid models still return 402. The free tier is
what makes generation possible, at zero cost.

## Defects found and fixed

**TEMP-001** (self-introduced, found on live Cipla data). A below-threshold
observation is stored as history and withheld from the timeline, but was still
carried forward as the yardstick. FY2027 would have been graded against a year
no reader can see — two inconsistent notions of "this year exists". Cipla
FY2026 scored 0.30 on three thin exchange filings.

**TEMP-002** (self-introduced, found on live Sun Pharma data). The free models
are **reasoning models**: they spend most of the completion budget on hidden
reasoning before emitting any JSON — one probe used 599 reasoning tokens to
produce a two-character answer. At a 4,000-token ceiling with a full evidence
block the reply was cut off mid-object, `json.loads` failed, and a complete
and correct set of findings was discarded and stored as a template fallback.
Sun Pharma FY2026's "fallback" was real analysis, mislabelled as fabricated
prose. Budget raised to 16,000 and the parser now repairs a truncated object,
dropping the incomplete trailing value rather than guessing it. Live fallbacks
fell from 3 to 1, and FY2026 recovered at 0.90 confidence.

**POLARITY-001.** "Debt improving" means debt **fell**. The contradiction
check used a single polarity, so every genuine deleveraging was flagged as a
narrative/accounts contradiction and every increase in borrowing read as
agreement — inverted on the dimension a credit analyst cares most about.

Tests **2,350 → 2,373**, zero failures, including regressions for all three.

## Caveats

- **Coverage is 3 companies** (Cipla, Sun Pharma, IOC) — the only ones with
  documents spanning more than one fiscal year. The engine is proven; the
  corpus is the constraint. The other 504 will produce observations as filings
  are collected.
- **No verdict has yet fired on live data.** The mechanism is verified on a
  controlled fixture and the discipline is verified live (the model correctly
  declined on Cipla), but no real company in the corpus yet has a year with
  explicit guidance followed by a year that settles it. That needs consecutive
  annual reports, not more code.
- **One fallback remains** (Cipla FY2027 v1, since superseded). The model
  occasionally returns pure prose with no JSON at all; that is recorded as a
  low-confidence fallback and flagged, never silently presented as analysis.
- **Free-tier models are rate-limited and can 429.** A sweep across many
  companies will need throttling; the three-company runs did not hit it.
- **Confidence is the model's own.** It is stored and displayed, not
  calibrated against outcomes. Once several years of verdicts accumulate, a
  calibration check becomes possible — whether 90%-confidence observations are
  right nine times in ten — but that requires history this corpus does not
  yet have.
- **No frontend surface.** The API is live and tested; no tab renders it.
