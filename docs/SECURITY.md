# Security

The service is a public, unauthenticated demo that spends a shared free-model quota and repeats third-party content. The threats below are ordered by how likely they are to matter. Each lists its controls and what they leave open.

## Threats and controls

### Prompt injection and jailbreaks

A user asks the model to ignore its rules, or a retrieved page carries instructions.

- The system prompt says document text is data, never instructions, and that the rules are private.
- Documents sit inside `<context>` with text and attributes HTML-escaped, so a page cannot close the wrapper and write outside it.
- Only `user` and `assistant` roles are accepted from the client. A smuggled `system` message is a 422.
- The model can only produce text. It has no tools, no browsing, and nothing it writes is executed, so a successful injection changes words on a screen and nothing else.
- Tested by `evals/redteam.yaml`: direct extraction, role play, an Arabic override, a planted "SYSTEM OVERRIDE" document, a forged assistant turn in the history.

Open: a determined user can still get an off-topic or oddly worded answer from a small free model. No output classifier runs, by choice: it would double the model calls on a quota of 1,000 a day.

### Invented facts presented as real

A real-estate answer with an invented price, phone number or rent period could mislead someone.

- The prompt forbids inventing prices, rent periods and contacts, and requires a `[n]` citation for every claim.
- Listing records keep only allowlisted fields. A rent with no stated period stays "period not stated".
- Every price is labelled with its snapshot date, and the UI shows the snapshot dates.
- The judge eval measured 0.99 to 1.00 of claims supported by the retrieved documents (`MODEL_CHOICE.md`). The red team checks that a CEO phone number and an exact current price are declined.

### Abuse and cost

Anyone can script requests and burn the shared quota, or send huge bodies.

| Control | Limit |
|---|---|
| Body size, checked before parsing | 128 KB, else 413 |
| Question length / messages per request | 2,000 characters / 13, else 422 |
| Control and bidi-override characters | rejected, 422 |
| Per client | 10 a minute, 100 a day, else 429 |
| Whole service, charged only after every other check passes | 60 a minute, 1,000 a day |
| Cloud Run | 2 instances × 40 concurrent requests, 60 s timeout |

The client key is the right-most `X-Forwarded-For` entry, the one Cloud Run's front end adds, so a client cannot choose its own key by sending the header. Verified on the live service: three requests with no header, a fake IP and a fake chain all logged the same client hash. IPv6 addresses are keyed by their /64, so rotating addresses inside one allocation does not reset the limit. Limiter memory is capped at 50,000 keys with least-recently-used eviction.

Open: limits live in each instance's memory, so with two instances a client can get up to twice the per-client limit, and the service-wide cap is per instance too. A shared store (Redis, Firestore) would fix it at the cost of a second service; at this traffic, the max-instances cap of 2 is the backstop.

### Optional passcode

Setting `ACCESS_PASSCODE` makes `/api/chat` require a matching `X-Access-Code` header, compared in constant time. It is off on the live demo so reviewers can test it directly.

Two reviews disagreed about wrong guesses. Locking the service after many wrong codes lets an attacker lock real users out; never locking lets guessing continue. The app takes availability: a correct code is always accepted, wrong codes share a service-wide budget (30 a minute, 500 a day), and startup refuses a passcode shorter than 20 characters. A random 20-character code is about 119 bits, so guessing it within the rate limits is not practical.

### Cross-site scripting through model output

The answer is untrusted text that can contain anything the model was induced to write.

- The UI builds answers from DOM nodes and `textContent`. It renders a small markdown subset (bold, code, lists, headings) itself and never uses `innerHTML` on model text.
- Links in answers are shown as text, not made clickable. Source links are made clickable only for `https` URLs.
- The Content Security Policy allows scripts, styles and connections from the site itself only, with no inline scripts or styles; images may also be `data:` URLs. It sets `object-src 'none'`, `base-uri 'none'`, `form-action 'self'` and `frame-ancestors 'none'`. Injected inline script would not run even if it reached the page.
- Also sent: HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, and a `Permissions-Policy` that turns off camera, microphone and geolocation. The OpenAPI schema and `/docs` are disabled.

### Secret leakage

The OpenRouter key is the one credential that costs money if leaked.

- In production the key lives in Secret Manager. The runtime service account `chat-runtime` can read that one secret and nothing else.
- Locally it lives in `.env`, which is git-ignored. `.env.example` holds a placeholder.
- No error message ever includes upstream details. The user sees "busy" for a 429 and a generic message otherwise; the detail goes to the log only.
- CI runs gitleaks over the full git history on every push and fails on any finding. The history is clean.
- GitHub Actions authenticates to GCP with Workload Identity Federation: no JSON keys exist to leak.

### Deployment pipeline

Whoever controls the pipeline controls the service and can read the secret.

- The identity pool accepts tokens only from `Sourav692/dar-wasalt-chat`.
- The read-only CI account (`gh-ci-reader`) can read the index bucket and nothing else. Any branch or pull request in the repo can use it; pull requests from forks get no token at all.
- The deploy account (`gh-deploy`) is bound to `ci.yml` running on `main`. A new workflow file, or `ci.yml` on another branch, cannot use it.
- Deploy runs only after lint, tests, the secret scan and the retrieval gate pass.
- Every third-party action and both base images are pinned to a commit SHA or image digest, so a moved tag cannot swap in other code. Dependabot proposes updates weekly.
- The container runs as a non-root user, with only runtime dependencies (no crawler, no dev tools).

Open: anyone who can push to `main` can change `ci.yml` and deploy. The repository has one maintainer. With collaborators, turn on branch protection with required reviews for `main`.

### Privacy of users

- Chat text is never logged or stored. Each request writes one JSON line: a salted hash of the client IP, status, model, latency, turn count, question length and filters.
- Uvicorn's access log is off; it would record IPs and paths.
- The browser keeps the conversation in memory; reloading the page clears it.
- Questions are sent to OpenRouter and on to the model provider. Free endpoints may log prompts or use them for training under their providers' terms. The page tells users it is an unofficial demo; do not type personal information into it.

Open: with `LOG_SALT` unset, each instance picks a random salt, so the same client gets different hashes on different instances. That is good for privacy and bad for tracing an abuser across instances. Set `LOG_SALT` as a secret if correlation is needed.

### Scraping ethics and third-party content

- The crawler obeys `robots.txt` (wildcards included, parsed with `protego`), sends an honest User-Agent, waits 5 seconds between requests, and fetches only allowlisted `https` hosts.
- It never solves or evades a challenge. Pages behind Cloudflare were opened by a person in a normal browser and saved; `DATA_COLLECTION.md` lists them.
- Listing extraction keeps only allowlisted fields: city, district, territory, property type, sale or rent, price, currency, rent period, area, bedrooms and the listing URL. Owner names, phone numbers and licence numbers never enter the corpus; a test plants them and checks they are dropped.
- The corpus and raw HTML are not in the public repository. They live in a private GCS bucket. The app answers with short cited passages and links back to the source page.

Open, argued rather than fixed: during a redirect, the crawler's browser can request an off-site URL before the scope check sees it. It runs on a laptop, never in production, holds no credentials, and off-site content is discarded before extraction, so the exposure is one request from a laptop.

## Reporting

This is a demo for a technical assignment. Report problems through the GitHub repository's issues.
