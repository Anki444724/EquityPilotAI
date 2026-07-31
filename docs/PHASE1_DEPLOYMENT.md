# Phase 1 — Railway deployment steps

Code is committed at `45c3cf4`. Deployment needs two things I could not do
without tokens: a push to GitHub, and three environment variables set on the
backend service.

## 1. Environment variables (backend service `6288c363-…`)

| Variable | Value | Required |
|---|---|---|
| `OPENROUTER_API_KEY` | the `sk-or-v1-…` key | **yes** |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | no — this is the default |
| `OPENROUTER_SITE_URL` | `https://frontend-production-1a313.up.railway.app` | no |
| `OPENROUTER_APP_NAME` | `EquityPilotAI` | no |

Set these in the Railway dashboard under the backend service → Variables, or
via the GraphQL API. **The key must exist only there** — not in the repository,
not in `.env` committed anywhere, not in a build argument.

`AI_MOCK_MODE` should stay `true`. It does not force template output; it keeps
the offline composer registered as the *last* resort, and the router places any
credentialled live provider ahead of it. Setting it `false` would turn a total
provider outage into a 500 rather than degraded-but-grounded prose.

## 2. Verifying the deployment

```bash
# is a live writer configured, and which one leads the chain?
curl -s https://backend-production-18956.up.railway.app/api/v1/ai/health | jq

# end-to-end, authenticated
export VERIFY_PASSWORD='…' VERIFY_EMAIL='ankitsingh835141@gmail.com'
python3 deploy/verify_live_modules.py \
  --url https://backend-production-18956.up.railway.app
```

The decisive check is `writer_mix` on a research report: it should read
`{"OpenRouter": N}`. Any `"Offline"` in that map means the key is missing or
rejected and the platform has silently degraded — which is precisely the
condition Phase 1 was raised to end.

```bash
curl -s -b cookies.txt \
  'https://backend-production-18956.up.railway.app/api/v1/company/TCS/ai/research-report' \
  | jq '{writer_mix, provider_mix, total_tokens, total_cost_usd}'
```

## 3. Cost

Measured, not estimated: a full fifteen-section report costs **US$0.0053–0.0055**
and about 27,000 tokens on `openai/gpt-4o-mini`. The supplied key carried a
$20 limit, so roughly 3,600 full reports. Section results are cached for 15
minutes, so a user re-reading a report does not pay twice.

## 4. Rollback

Setting `AI_PREFERRED_PROVIDER=Gemini` moves Gemini to the head of the chain
without a deploy. Removing `OPENROUTER_API_KEY` drops OpenRouter out entirely
and the platform falls back to Gemini, then to the offline composer — degraded
prose, but the routing, citations and confidence scoring are unaffected because
none of them depend on the writer.
