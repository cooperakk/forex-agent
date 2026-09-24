# AI assistants: providers, keys, and what a model is allowed to do

Sentinel-FX can use large language models from several providers. It runs
exactly as before with none configured: every AI feature is optional and every
failure degrades to "no AI", never to "no trading" or "more risk".

## What a model may and may not do

| A model MAY | A model may NOT |
|---|---|
| Read official central-bank headlines and flag a **correction** (shrinks size) or **contradictory reporting** (blocks the currency for up to 4 h, only at confidence >= 0.6) | Open, enlarge or keep open a position |
| Explain a closed trade in plain Persian, categorise it, and suggest ONE hypothesis to test | Change a risk limit, a stop, the mode, or a strategy's parameters |
| Write a daily market brief from facts the system already holds | Promote a strategy or bypass the acceptance protocol |

The statistical learning loop (post-mortems -> BH-corrected lessons -> proposals
that wait for a human and a validation run) remains the **only** part of the
system that can change behaviour. A model's opinion is displayed and counted,
never treated as evidence.

## Supported providers

| Id | Provider | API | Default model | Where to get a key |
|---|---|---|---|---|
| `anthropic` | Claude (Anthropic) | Messages API | `claude-sonnet-5` | console.anthropic.com |
| `openai` | ChatGPT (OpenAI) | Chat Completions | `gpt-5-mini` | platform.openai.com/api-keys |
| `gemini` | Gemini (Google) | generateContent | `gemini-2.5-flash` | aistudio.google.com/app/apikey |
| `deepseek` | DeepSeek | OpenAI-compatible | `deepseek-chat` | platform.deepseek.com/api_keys |
| `kimi` | Kimi (Moonshot AI) | OpenAI-compatible | `kimi-k2-turbo-preview` | platform.moonshot.ai (use `https://api.moonshot.cn/v1` for China accounts) |
| `custom` | Any OpenAI-compatible server | OpenAI-compatible | -- | e.g. Qwen, Grok, OpenRouter, or a local model at `http://127.0.0.1:.../v1` |

Model names change often. After saving a key, press **Test connection** on the
dashboard: it makes a tiny call and lists the models the provider actually
offers your key, so you can pick one instead of guessing.

## Configure

Dashboard -> **هوش مصنوعی و اخبار** (owner only, every change needs a TOTP code):

1. Enter the key for one or more providers, choose the model, enable them.
2. Choose the **primary** provider and optional **fallbacks** (tried in order
   when the primary fails or times out).
3. Switch each purpose on or off: official news, the trade coach, the brief.
4. Set the hourly and daily call budgets (defaults: 60 / 400). A runaway loop
   costs a refused call, not an invoice.

## Security properties

* **Keys are sealed** with AES-256-GCM in `var/ai-secrets.json`, using the same
  key as the broker credentials (`SENTINEL_SECRET_KEY` or
  `var/broker-secrets.key`). The API never returns a key -- only whether one is
  stored and, to the owner, its last four characters.
* **No redirects are followed**: a redirect would carry the key to whatever
  host the `Location` header names.
* A **custom endpoint must be https** (plain http only to loopback, for a local
  model) and may not contain credentials, a query or a fragment.
* Provider **error bodies are redacted** before they are logged or shown, in
  case they echo the key.
* Responses are **size- and time-capped**; model ids are validated because
  Gemini's travels in the URL path.
* **Every call is journalled** in the tamper-evident audit chain (`ai.call`):
  provider, model, purpose, latency, tokens and a hash of the prompt -- never
  the prompt or the answer, which may contain account details.
* **Prompt injection** is contained by construction: only official headlines
  reach a model as untrusted text, the output is schema-validated, quotes must
  appear verbatim in the source (fabrications are dropped), and the only
  effects available are shrink/block. The coach and the brief see only data
  the system produced.
* The licence capability `llm_news` gates all AI use.

## News sources

* **Economic calendar**: the Forex Factory weekly export. Confirmed release
  times create real blackout windows around high-impact events (the bundled
  schedule is a recurrence *pattern* and by design never blocks).
* **Official feeds**: Federal Reserve, ECB, Bank of England, Bank of Japan,
  Reserve Bank of Australia, Bank of Canada, US Bureau of Labor Statistics.
  HTTPS only, no redirects, 2 MB cap, and any XML declaring a DTD or entity is
  refused before parsing.

Both need outbound HTTPS from the engine host. Turn them off with
`news.live_calendar = false` / `news.official_feeds = false`.
