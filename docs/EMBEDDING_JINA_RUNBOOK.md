# Embedding provider runbook: jina-v3 (OpenRouter removed)

Why this exists: the OpenRouter account backing the old default embedding
provider (`bge-m3`) is exhausted and returns **HTTP 402**. OpenRouter must not
be used. Semantic embeddings now come from **jina-v3**, selected through the
existing `EMBEDDING_PROVIDER` mechanism — no new RAG architecture, no schema
change, no manual database work.

## Provider selected: jina-v3

| Candidate | Verdict | Reason |
|---|---|---|
| `jina-v3` (jina-embeddings-v3) | ✅ **selected** | Free tier (no credit card, ~1M tokens/month), multilingual, 1024 dimensions = fits the production `vector(1024)` column, already implemented in `app/services/retrieval/embeddings.py` |
| `bge-m3` (baai/bge-m3 via OpenRouter) | ❌ rejected | Needs OpenRouter credits; the account returns HTTP 402 and must not be used |
| `openai-small` (text-embedding-3-small) | ❌ rejected | Needs a paid OpenAI key (no free tier) AND returns 1536 dimensions, which do not fit the `vector(1024)` column without a migration |
| Gemini embeddings (text-embedding-004 / gemini-embedding-001) | ❌ rejected | 768 / 3072 dimensions do not match the 1024 column, and the existing transport sends no `dimensions` parameter — adopting it would mean a new provider class plus a column migration, i.e. a new architecture. The task requires using an existing supported provider instead |

## Exact environment changes (Railway, api + worker services)

```bash
EMBEDDING_PROVIDER=jina-v3
JINA_API_KEY=<free key from https://jina.ai/>   # free tier, no credit card
OPENROUTER_API_KEY=                             # LEAVE EMPTY — unset the dead key
```

Keep unchanged:

```bash
AI_PREFERRED_PROVIDER=Gemini
GEMINI_API_KEY=<existing>
HYBRID_RETRIEVAL_ENABLED=true
```

`OPENROUTER_MODEL`, `OPENROUTER_SITE_URL` and `OPENROUTER_APP_NAME` become
inert once the key is empty; they can stay or be removed. The LLM fallback
order is now `Gemini → OpenAI → Claude → OpenRouter → Offline`, so even a
stale OpenRouter key is tried last rather than first.

Apply to **both** the `api` and `worker` services: the api embeds queries at
request time and the worker embeds chunks during the backfill — they must use
the same provider or query and stored vectors will be in different spaces.

## What happens on deploy (no action needed)

1. **Queries** embed with jina-v3 immediately. Retrieval keeps working
   throughout: lexical, metadata and temporal signals are unaffected, and the
   hybrid engine degrades gracefully whenever the semantic signal is missing.
2. **Existing stored embeddings stay untouched.** Old `bge-m3` vectors remain
   in `embedding_v2` until the backfill overwrites them row by row. Nothing is
   deleted, no column is dropped, no job is reset. **Do not reset
   DocumentJob #976** and do not touch the database — ingestion jobs are
   unrelated to this switch.
3. **The scheduled backfill re-embeds incrementally.** A re-embed is required
   (vectors from different spaces must not be compared — a cosine across
   spaces is arithmetically valid and meaningless), and the existing
   `EMBEDDING_BACKFILL` schedule does it 500 chunks per run with no downtime.
   Until a chunk is re-embedded, its semantic score compares a jina-v3 query
   against a bge-m3 vector; lexical + rerank carry retrieval during this
   window, exactly as they do during any provider outage.

## Verification (read-only; safe to run)

```sql
-- How many chunks still carry the old space (shrinks to 0 as the backfill runs)
SELECT count(*) FROM document_chunks
WHERE embedding_spec_v2 IS DISTINCT FROM 'jina-v3:jina-embeddings-v3:1024';
```

```bash
# LLM: Gemini serves, OpenRouter is last or absent from the chain
curl -fsS https://<app>.up.railway.app/api/v1/ai/health | jq '.serving, .degraded'

# Public chat still answers quickly (regression: was a 45s timeout)
time curl -fsS -X POST https://<app>.up.railway.app/api/v1/blogger/chat \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"SHRIRAMFIN","question":"What did the latest post say?","session_id":"verify"}' \
  | jq '.grounded, (.citations | length)'
```

Watch the logs for `embedding backfill` lines (`provider` should read
`jina-v3`) and for the absence of new `HTTP Error 402` lines from `bge-m3`.

## Backfill sizing

`DEFAULT_LIMIT` is 500 chunks per run. Divide the pending count above by 500
to get the number of runs; runs are on the existing half-hour schedule, so
e.g. 10,000 chunks ≈ 20 runs ≈ 10 hours. No intervention is needed — an
interrupted run resumes from state, and the vector index rebuilds
automatically when the last chunk lands.

## Rollback

If Jina is unreachable, set `EMBEDDING_PROVIDER=bge-m3` **only** after topping
up the OpenRouter account — otherwise the provider 402s and retrieval serves
lexical-only (safe, but not semantic). There is no code rollback needed: both
providers remain implemented behind the same setting.
