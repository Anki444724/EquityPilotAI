# Blogger Integration

Posts from `equitypilot.blogspot.com` become searchable, citable knowledge for
the platform's existing AI — and a public chatbot the blog can embed.

```
Blogger feed ──► BloggerSyncService ──► DocumentIngestionService.accept()
                                              │
                        the platform's existing document worker
                                              │
              parse ► chunk ► embed ► index ► knowledge ► retrieval
                                              │
                        the platform's existing ResearchAnalyst
                                              │
              POST /api/v1/blogger/chat  ◄────┴────► /company/{ticker}/ai/chat
                 (public, allowlisted)              (authenticated, unchanged)
```

Everything after `accept()` already existed. The Blogger package decides two
things and does nothing else: **which company a post is about**, and **whether
the platform already has this version of it**. Parsing, chunking, embedding,
retrieval, knowledge enrichment, guardrails and the Language Adapter are the
platform's, shared with uploads and collected filings. There is no second index,
no second worker and no second AI.

---

## 1. What was added

| Path | Purpose |
|---|---|
| `backend/app/services/blogger/feed.py` | Paged Atom reader, post model, content fingerprint |
| `backend/app/services/blogger/mapping.py` | Post → company resolution, with refusals |
| `backend/app/services/blogger/document.py` | Stable filename, provenance metadata, HTML render |
| `backend/app/services/blogger/sync.py` | Orchestration + `python -m app.services.blogger.sync` |
| `backend/app/api/v1/blogger.py` | `POST /blogger/chat`, `GET /blogger/status`, `POST /blogger/sync` |
| `backend/app/schemas/blogger.py` | The public response surface (deliberately small) |
| `frontend/public/blogger-chat-widget.js` | Embeddable widget — one `<script>` tag, no build step |
| `backend/tests/test_blogger_sync.py` | 119 tests: feed, identity, mapping (incl. the live feed's own posts), sync, wiring, CLI |
| `backend/tests/test_blogger_api.py` | 49 tests: restrictions, response surface, status, trigger, the live Shriram Finance post |
| `backend/tests/fixtures/blogger_feed.py` | A feed built the way Blogger actually serves one |

Changed, not added:

| Path | Why |
|---|---|
| `backend/app/core/config.py` | Seven `BLOGGER_*` settings, no secret defaults |
| `backend/.env.example` | Documents them (a setting without an entry fails CI) |
| `backend/app/domain/documents/types.py` | `ParsedDocument.metadata`; `DOCUMENT_TYPE_LABELS` |
| `backend/app/services/documents/extractors/office.py` | `HtmlParser` reads `<meta name/property>` tags |
| `backend/app/services/documents/service.py` | `_persist` **merges** metadata instead of overwriting |
| `backend/app/services/documents/ingestion.py` | `accept(..., metadata=...)`; refresh on duplicate |
| `backend/app/domain/platform/jobs.py` | `JobKind.BLOGGER_SYNC` + label, priority, retry, 6-hour schedule |
| `backend/app/services/platform/jobs/handlers.py` | The handler; registered in `HANDLERS` |
| `backend/app/domain/platform/limits.py` | `blogger.chat` (12/min/IP) and `blogger.sync` (6/10min/IP) |
| `backend/app/services/ai/analyst.py` | Evidence labels name the document's real kind |
| `backend/app/services/ai/providers/mock.py` | **Bug fix**: an evidence quotation could orphan its own citation (see §8) |
| `backend/app/services/language/adapter.py` | **Bug fix**: frozen-dataclass mutation (see §8) |
| `backend/app/domain/language/detect.py` | **Bug fix**: `"the"` as a Hindi marker (see §8) |
| `backend/app/api/v1/router.py` | Registers the router ahead of greedy routes |
| `backend/app/api/v1/broker_angelone.py` | Docstring no longer claims the only public endpoint |
| `docker-compose.yml` | Blogger env for `api` + `worker`; blog origin in CORS |

---

## 2. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `BLOGGER_ENABLED` | `false` | Master switch. Off means no sync, no public chat. |
| `BLOGGER_FEED_URL` | `""` | The Atom feed. `https://equitypilot.blogspot.com/feeds/posts/default` |
| `BLOGGER_PUBLIC_TICKERS` | `""` | Comma-separated allowlist for the public chatbot. Empty publishes nothing. |
| `BLOGGER_MAX_POSTS` | `100` | Ceiling per run, newest first. |
| `BLOGGER_SYNC_TIMEOUT_SECONDS` | `30.0` | Per feed request. |
| `BLOGGER_CHAT_TIMEOUT_SECONDS` | `45.0` | Wall-clock budget for one public answer. Expiry ⇒ retryable `504`. Clamped to 1–300. Keep below the proxy's read timeout. |
| `BLOGGER_DEFAULT_TICKER` | `""` | Fallback company. **Leave empty** unless every post is about one company. |
| `BLOGGER_SYNC_SECRET` | `""` | Credential for `POST /blogger/sync`. Empty ⇒ signed-in operator only. |

Nothing is hard-coded. `BLOGGER_SYNC_SECRET` is compared with
`hmac.compare_digest`, never logged, never returned, and an **empty** value does
not accept an empty header — that comparison is skipped entirely rather than
succeeding.

`BLOGGER_ENABLED=false` is the safe default: a deployment that pulls this code
without configuring it behaves exactly as it did before.

---

## 3. How a post is matched to a company

Four tiers, tried in order. The first that produces exactly one company wins.

1. **Ticker label** — a label that *is* a ticker, whole-token: `TCS`, `NESTLEIND`,
   `BEL`. Leading words may be concatenated (`JSW Steel Quarterly` → `JSWSTEEL`),
   but only past four characters, so no two-letter fragment is ever invented.
2. **Company-name label** — normalised (accents folded, `&` read as `and`, legal
   suffixes dropped) and matched **by dictionary lookup on whole phrases**, never
   by substring scan. `Nestlé India` → `NESTLEIND`; `L&T` → `LT` via an alias
   table; `Zomato` → `ETERNAL` the same way.
3. **Title** — the same phrase matching applied to the post title.
4. **Default ticker** — only if `BLOGGER_DEFAULT_TICKER` is set.

Guarantees:

* **No substring matching.** `ITC` does not match *switching*, *pitcher* or
  *literacy*. Words are tokenised and looked up whole.
* **Specificity wins.** With both `ITC Ltd` and `ITC Infotech` in the register, a
  label reading `ITC Infotech` resolves to the subsidiary, not the parent —
  because a candidate contained in a longer matching phrase is discarded.
* **Ambiguity is a refusal.** Two equally specific companies ⇒ the post is
  skipped and logged, never picked at random.
* **Ordinary words are not companies.** `Finance`, `Bank`, `Steel`, `India` are
  refused as single-word *name* matches (they still work as tickers and aliases),
  so a label reading `Finance Stocks` cannot file a post against whichever lender
  happens to have that word in its registered name.
* **A rename is one company, not an ambiguity.** An import that matches on ISIN
  leaves the old row beside the new one — `ZOMATO` delisted and `ETERNAL` active,
  both named *Eternal Ltd* — and a label like `"Eternal Ltd Stock Analysis:
  Zomato"` reaches both. Two rows for one company are collapsed onto the active
  listing. Two *active* rows sharing a name are still reported as ambiguous: that
  is a register problem, and choosing silently would hide it.
* **Soft-deleted companies are invisible.**
* **An unmapped post is skipped, logged and left alone** — with its labels and
  the reason in the log line, which is what an operator needs in order to add the
  missing alias or label.

---

## 4. Repeatability

A post's filename is its identity: `blogger-<post_id>.html`, derived from the
Atom entry id, never from the title or the URL (both of which an author can
change).

| Feed state | Action |
|---|---|
| Post not stored | `accept()` → new document, version 1, one job enqueued |
| Content fingerprint matches | Nothing. No row, no job. |
| Feed timestamp matches | Nothing. No row, no job. |
| Content differs | `accept()` → version *n+1*; the old row gets `superseded_by` |
| Byte-identical after `--force` | `accept()` returns `duplicate`; provenance refreshed |
| Stored document is `FAILED` | `reprocess()` — re-indexed from its own stored source |
| Post now maps elsewhere | New row under the new company; the old row is superseded |
| Post vanished from the feed | Nothing. **Deletion is never inferred from absence.** |

Two independent change signals, because they fail differently: the fingerprint
(title + labels + whitespace-normalised body) ignores a re-publish that touched
nothing, and the timestamp is cheap enough to compare on every run without
reading a body. A document carrying neither — ingested before this existed — is
treated as changed and left to the content hash, so unknown provenance costs one
comparison rather than a skipped post.

**Nothing is ever deleted**: not a document, chunk, fact, graph edge or company.
Supersession is the platform's own mechanism for a new version of an annual
report, and it keeps a citation issued last month resolvable.

There are no migrations. Provenance lives in the existing `doc_metadata` JSON
column, which is why nothing here can be destructive.

---

## 5. The public chatbot

`POST /api/v1/blogger/chat` — the only AI route an anonymous caller reaches.

```json
{ "ticker": "SHRIRAMFIN", "question": "AUM kitna hai?", "session_id": "public", "language": "auto" }
```

It runs the same `ResearchAnalyst`, over the same retrieval, the same citation
audit, the same guardrails and the same Language Adapter as
`/company/{ticker}/ai/chat`. English, Hindi and Hinglish questions are detected
and answered in kind. When nothing in the corpus bears on the question the reader
is told so — the source router refuses fail-closed — and an answer with no
document evidence behind it carries an explicit note saying it came from computed
figures alone.

Controls, in the order they are applied:

1. Rate limit: 12 requests/minute/address (below the global anonymous ceiling of
   60/min).
2. Feature switch: `BLOGGER_ENABLED`.
3. Ticker allowlist: `BLOGGER_PUBLIC_TICKERS`. Checked **before** any lookup, so
   an unlisted ticker cannot provoke a database read.
4. Company lookup with `provision=False`: an anonymous caller cannot make this
   platform perform outbound I/O or create rows by naming a symbol.

The response carries an answer, `grounded`, citations (each with the post's own
URL, title, kind, page and a quoted snippet capped at 400 characters), warnings,
the disclosure and the language block.

`grounded` means what a reader of a research blog takes it to mean: the answer
rests on retrieved evidence. The citation audit's own verdict is narrower — no
uncited number, no invented key — and an answer that declines for want of
evidence satisfies it vacuously, citing nothing and claiming nothing. The public
field therefore also requires citations to have been retrieved; a widget that
rendered `grounded: true` beside *"I found no evidence"* would be telling the
reader the opposite of the truth. It does **not** carry `provider`, `model`,
`prompt_key`, `prompt_version`, token counts, `cost_usd`, `data_quality`,
`document_id` or `chunk_id`. Errors are generic and the detail goes to the log: a
provider's own error text can name a model, a base URL or part of a request.

Conversation memory is keyed `blogger-public:<address>:<session_id>`. The session
id is caller-chosen and therefore not an identity — without the address in the
key, two readers who both sent `"public"` would share one conversation.

`GET /api/v1/blogger/status` tells the widget what it may offer: `enabled`, a
reason, the published tickers with an indexed-post count, when the corpus last
changed, and the disclosure. When the feature is off it advertises nothing, so
the widget hides itself instead of rendering a box that can only error.

`POST /api/v1/blogger/sync` queues one pass on the existing worker — queued, not
run inline, because a full pass outlives a sane HTTP request. Authorised by the
`X-Blogger-Sync-Secret` header **or** by a signed-in principal holding
`document:upload`, and audited as `system.job.enqueued`. Every refusal returns the
same message, so the endpoint does not disclose whether a credential exists.

No admin surface is reachable from any of these routes, and none of them accepts a
tenant, user or document id from the caller.

---

## 6. Deploying

`docker-compose.yml` already carries the Blogger block for both `api` and
`worker`. Postgres, Redis and every volume are untouched.

```bash
# on the EC2 host, in the repository
git fetch origin && git checkout main && git pull --ff-only
docker compose build api worker
docker compose up -d api worker

# watch the first scheduled pass, or force one now
docker compose exec worker python -m app.services.blogger.sync --dry-run
docker compose exec worker python -m app.services.blogger.sync
```

Before the first run, set in the deployment's environment (not in the
repository):

```bash
BLOGGER_ENABLED=true
BLOGGER_FEED_URL=https://equitypilot.blogspot.com/feeds/posts/default
BLOGGER_PUBLIC_TICKERS=SHRIRAMFIN
BLOGGER_SYNC_SECRET=<generate one>
BLOGGER_CHAT_TIMEOUT_SECONDS=45   # below nginx/ALB proxy_read_timeout
CORS_ORIGINS='["https://equitypilot.in","https://www.equitypilot.in","https://equitypilot.blogspot.com"]'
```

The blog origin is listed explicitly. A wildcard with `allow_credentials=True` is
either ignored by the browser or an open door; neither is acceptable.

CLI exit codes: `0` clean, `1` the run had failures or could not read the feed,
`2` it did not run because the feature is off. Distinguished so a cron job can
alert on `1` and stay quiet on a deployment that has not switched the feature on.
`--json` prints the machine-readable summary; the default output lists every
skipped and failed post with its reason.

The scheduler runs `BLOGGER_SYNC` every six hours on the existing worker, at
`BACKGROUND` priority, with two retries five minutes apart. Posts it accepts
become `DOCUMENT_PROCESSING` jobs, which the existing document worker drains at
its own priority — so a hundred-post first sync cannot delay an interactive
upload.

### The widget

```html
<script src="https://equitypilot.in/blogger-chat-widget.js"
        data-api="https://equitypilot.in/api/v1"
        data-ticker="SHRIRAMFIN"
        async></script>
```

Optional attributes: `data-heading`, `data-hint`, `data-accent`. With more than
one published ticker and no `data-ticker`, the widget renders a company picker.
It holds no credential, sends none, writes nothing to `localStorage`, and inserts
model output with `textContent` rather than `innerHTML` — an answer is untrusted
text, and rendering it as markup would let a passage quoted from a document
execute in the blog's origin.

---

## 7. Operating it

**A post was skipped.** The log line carries the labels and the reason:

```
blogger post not mapped to a company  post_id=485013…  labels=['NBFC','Shriram Fin']
  reason="no ticker label, company-name label or title mention matched a company in the database"
```

Fix it by labelling the post with its NSE ticker in Blogger — the cheapest and
most reliable signal — or by adding the name to `Company.aliases`, or, for a
rename, one entry in `ALIASES` in `app/services/blogger/mapping.py`. Then re-run
with `--force`.

**The feed is unreachable.** The job raises, the queue retries twice at five
minutes, and the corpus is left exactly as it was. Nothing is marked failed: an
unreachable feed is not evidence that a post disappeared.

**A document failed to index.** The next sync sees the `FAILED` row and calls
`reprocess()`, which re-reads the stored source. That is why the original bytes
are kept, and why a failed Blogger document needs no manual intervention.

**Useful log lines:** `blogger feed read`, `blogger sync complete`,
`blogger post ingested`, `blogger post not mapped to a company`,
`blogger post moved company; previous document superseded`,
`feed paging did not advance`.

Operational notes:

* Per-address rate limiting reads `X-Forwarded-For`. On a public endpoint that is
  only as trustworthy as the proxy in front of it — the reverse proxy must
  overwrite the header, not append to it, or a caller can rotate their own bucket.
* The conversation store is in-process with a bounded capacity, so a caller
  sending many distinct `session_id`s can evict other readers' threads. It cannot
  read them: keys are namespaced by address.
* The public endpoint records usage like every other answer, which is how the
  operator learns what the blog costs. The rate limit is what bounds it.

---

## 8. Three pre-existing bugs this surfaced

All three were found by pointing an ordinary question at the chat over a real
Blogger post, and all three are fixed rather than worked around, because the
public endpoint inherits them — and so does the authenticated one.

**`FrozenInstanceError` on any mixed-language question.**
`LanguageAdapter.normalise_query` wrote to a frozen `Detection`
(`detection.is_mixed = True`). Every question the mixed-language detector flagged
crashed the request — a 500 from the chat endpoint, several frames from its
cause. Now built with `dataclasses.replace`.

**`"the"` was a Hindi marker.** `_HINDI_MARKERS` listed `tha thi the thay` as
romanisations of था/थी/थे. `the` is also the most common word in English, so a
question using it twice scored 4.0 against a threshold of 2.5 and was classified
Hinglish — which then ran the adapter, which then hit the bug above. The module
already documented the policy (*"Deliberately EXCLUDES words that are also
ordinary English … an English speaker must never be answered in Hinglish"*) and
listed `he`, `to`, `so`, `me`, `is`, `hi`, `do`, `in`, `at`, `ho`; `the` was the
one that got through. Removed. Its siblings stay: they are not English words.

**A fully cited answer was reported as ungrounded.**
The offline provider composes `"The platform's figures put X at <evidence>
[key]"`. For a computed figure the evidence is a number; for a retrieved passage
it is prose that ends its own sentences — and the live post's first chunk ends
with the heading *"What Does Shriram Finance Do?"*. The citation audit splits on
`.!?`, so the sentence it measured stopped at that question mark, one fragment
short of the marker that supports it: coverage 1/2, `grounded: false`, warning
*"Only 50% of numeric statements carry a citation"* — on an answer with no
unknown key and no fabricated number. Evidence quoted inside a sentence now has
its terminators folded to semicolons and is capped at 320 characters, so the
marker stays with the figures it vouches for. Coverage 2/2, `grounded: true`.

**A public request timed out after the work was already done.**
Everything upstream of the model is fast — sync, worker, retrieval, grounding and
the citation audit complete in well under a second, and a live `POST
/api/v1/blogger/chat` for `SHRIRAMFIN` returned in 0.163 s. What is *not* fast is
the provider chain behind `analyst.chat`: `ProviderRouter` retries each
configured provider `MAX_ATTEMPTS = 3` times at a `timeout_seconds = 60.0` HTTP
timeout, with backoff between attempts, and then falls through to the next
provider in a four-deep `FALLBACK_ORDER` — **182 s with one provider configured,
726 s with all four**. The public endpoint inherited that patience whole, so a
slow or unreachable model left an anonymous reader's connection open for minutes
while nginx or an ALB (60 s default `proxy_read_timeout`) had given up long
before: the answer existed on the server and was lost on the way out, which the
reader experiences as a hang.

Two changes, both contained to the public endpoint:

1. `asyncio.wait_for(analyst.chat(...), timeout=settings.blogger_chat_timeout_seconds)`
   — a budget of its own (45 s default, configurable, clamped 1–300). Expiry logs
   the ticker, the budget and the elapsed time, and returns **504** with wording a
   reader can act on. Nothing is recorded for an answer that never arrived.
   Demonstrated live: with the model slowed to 8 s and a 3 s budget, the endpoint
   returned `504` in 3.13 s instead of waiting.
2. The blocking database phases — the company lookup, the usage record, the
   citation enrichment — moved into `fastapi.concurrency.run_in_threadpool`. The
   handler is `async def`, so run on the loop that work stalls every other
   reader; a cold company lookup measured ~90 ms. Probed live: `GET
   /blogger/status` returned in 11 ms *while* a chat was mid-generation.

The authenticated `/company/{ticker}/ai/chat` is untouched — a signed-in analyst
waiting on a report should get the router's full patience.

All four fixes are covered by the existing multilingual suite (190 tests), the
AI and retrieval suites (346 tests) and the Blogger tests (173).

---

## 9. What did not change

The financial database, company register, uploads, PDF/HTML ingestion, chunking,
embeddings, vector store, hybrid retrieval, knowledge vault, memory enrichment,
`ContextBuilder`, `AIService`, `ResearchAnalyst`, scoring, valuation, forecasting,
reports, portfolios, market data, the broker integration, authentication,
authorisation, the worker, the scheduler, the queue and Docker's Postgres, Redis
and volumes.

`/company/{ticker}/ai/chat` keeps its full response surface — provider, model,
prompt version, token counts, cost, citation audit, guardrails, data quality —
and `tests/test_blogger_api.py` asserts that it does, so the public variant
cannot quietly become the shared one.
