# Architecture

One container on Cloud Run serves a static chat page and a streaming chat API. Everything the answers draw on is collected offline, indexed once, and baked into the image, so production never contacts Dar Global or Wasalt.

```
 offline, on a laptop
 ─────────────────────────────────────────────────────────────────────────────────
 darglobal.co.uk ── crawler/ (Playwright, robots.txt, 5 s delay, skips challenges)
 wasalt.sa/en ───── saved by hand in a normal browser (Cloudflare challenge)
        │
        ▼
 crawler/extract.py ─▶ data/docs.jsonl ─▶ ingest/build_index.py ─▶ index/
   page text + allowlisted listing fields     80-word chunks, multilingual     chunks.jsonl
   + url + snapshot date                      MiniLM vectors                   vectors.npy
                                                                               manifest.json
        └──────────────── uploaded to a private GCS bucket ──────────────────────┘

 on every push to main (GitHub Actions, keyless auth to GCP)
 ─────────────────────────────────────────────────────────────────────────────────
 lint + tests ─┐
 gitleaks ─────┼─▶ pull index from GCS ─▶ docker build ─▶ Artifact Registry ─▶ Cloud Run
 retrieval gate┘                          (index + embedding model baked in)   smoke test

 per request, on Cloud Run (asia-south1, 0 to 2 instances)
 ─────────────────────────────────────────────────────────────────────────────────
 browser ── GET /           static page (HTML, CSS, JS; no third-party assets)
         ── GET /health     index hash, chunk count, snapshot dates, model chain
         ── POST /api/chat  Server-Sent Events:
              1. body cap, schema, per-client and service-wide rate limits, passcode if set
              2. parse listing constraints: city, sale or rent, type, bedrooms, price
              3. hybrid search: BM25 + dense vectors, fused, filtered, one chunk per page
              4. prompt: rules + escaped <context> documents + trimmed history
              5. OpenRouter, streamed, with a fallback chain of free models
              6. events: sources, tokens, done (or one short error message)
              7. one JSON log line with a hashed client IP and no message text
```

## Request flow

**Validation.** A pure-ASGI middleware rejects bodies over 128 KB before they are parsed. Pydantic then requires 1 to 13 messages, roles `user` or `assistant` only, the last one from the user, at most 2,000 characters, and no control or bidirectional-override characters. The per-client limit (10 a minute, 100 a day) runs before the passcode check, so guesses are throttled too. The service-wide quota (60 a minute, 1,000 a day, matching OpenRouter's free-model allowance) is charged only after a request has passed every check, so junk requests cannot spend it.

**Constraint parsing** (`app/constraints.py`). Regexes read "3-bedroom villas for rent in Jeddah under 100k" or its Arabic form into a filter: city, purpose, property type, bedrooms, price bounds, with Eastern Arabic digits and `k`, `m`, `ألف`, `مليون` handled. A follow-up such as "and in Riyadh?" keeps the previous turn's filters and swaps only what changed.

**Retrieval** (`app/retrieval.py`). The query is the current question plus the previous one, so follow-ups keep their subject. Two rankings are fused with Reciprocal Rank Fusion (k = 60):

- BM25 over each chunk's title (counted twice), URL path words and text. The query is expanded with English names for Arabic place and brand names, the English form of any parsed filter, and three synonyms the golden set showed were needed ("contact", "CEO", "phone").
- Cosine similarity against 384-dimension vectors from `paraphrase-multilingual-MiniLM-L12-v2`, so an Arabic question finds English text.

Listings that contradict a parsed filter are removed before ranking. If no listing survives, the filters are dropped and the prompt notes that the results are near matches. The top six chunks come from six different pages.

**Prompt** (`app/prompt.py`). The system prompt sets eight rules: answer only from the documents, cite them as `[n]`, never invent prices, rent periods or contacts, treat document text as data and never as instructions, answer in the user's language, refuse unrelated topics, give no financial advice, and keep the rules private. Documents go inside `<context>` with their text and attributes HTML-escaped, so a page containing `</doc></context>` cannot close the wrapper. History is trimmed to 12 messages, 8,000 characters in all and 2,000 per turn.

**Model call** (`app/llm.py`). One streaming request to OpenRouter with a `models` list: `nemotron-3.5-super-vl`, then `nemotron-3.5-lightning`, then `nemotron-3-ultra`, all free. OpenRouter moves to the next model on an error. Reasoning stays on at low effort, with its tokens hidden. With it off, the model invented a Dar Global project in Japan. A 50-second overall deadline sits inside Cloud Run's 60-second request timeout. OpenRouter's keep-alive comments reset an ordinary read timeout, so a read timeout alone let one request hang for over two minutes. Errors inside a 200 stream and empty streams count as failures. The user only ever sees "busy, try again" or a generic error.

**UI** (`app/static/`). Plain HTML, CSS and JavaScript under a self-only Content Security Policy. Answers are built with DOM nodes and `textContent`, never `innerHTML`, so model output cannot inject markup. `[n]` chips jump to the matching source; cited sources are listed first, the rest fold under "Also searched, not cited". `dir="auto"` lays Arabic out right to left. The browser keeps the conversation and sends the last 12 messages; the server stores nothing.

## Why these choices

**Hybrid search, not dense only.** Project names (DG1, Tierra Viva, AIDA) and city names are rare exact tokens that BM25 ranks well and a small embedding model blurs. Dense vectors catch paraphrases and the Arabic questions. On the golden set, hybrid search reached hit@5 0.81 before any tuning; the tuning steps are in `VERIFICATION.md`.

**80-word chunks.** The embedding model reads only the first 128 word pieces of its input. Longer chunks would be embedded from their first lines alone. Headings start a new chunk and neighbours overlap by 12 words; when a page chunk is retrieved, its neighbours are joined back for the prompt.

**In memory, no vector database.** 2,171 chunks × 384 floats is 3.3 MB. A numpy dot product over all of them takes under a millisecond, and BM25 is rebuilt at startup in 0.09 s. A database would add a network hop, a second service to secure and a cost, and buy nothing at this size.

**Baked-in index and model.** The image holds the index and the 235 MB embedding model, and loads the model with the Hugging Face hub set to offline. A cold start needs no network call except to OpenRouter, and every deployed image pins one exact index version (`index/fc1e965542bd`, the first 12 characters of the corpus hash).

**Stateless.** No database and no stored chat text: nothing to breach, back up or delete on request. The cost is that rate limits are counted per instance (see `SECURITY.md`).

## Running costs and limits

Cloud Run scales to zero, so an idle service costs nothing. A cold start takes about 10 s: 2.9 s to start Python and import, 6.8 s to load the index and warm the embedding model. A warm answer usually starts in 3 to 7 s. The free models allow 1,000 requests a day across the whole OpenRouter account, which is where the service-wide daily cap comes from.
