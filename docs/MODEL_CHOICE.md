# Model choice

The app calls OpenRouter with a fallback chain, primary first:

```
nvidia/nemotron-3.5-super-vl:free
nvidia/nemotron-3.5-lightning:free
nvidia/nemotron-3-ultra-550b-a55b:free
```

OpenRouter tries the next model when one returns an error (a 429, a 5xx, a provider outage). Set `OPENROUTER_MODELS` to change the chain without a code change.

## Candidates

OpenRouter listed 14 free models on 2026-11-02. Five were dropped without a test: a content-safety classifier, a code model, a 2.6B model, and two models from vendors with no track record on the platform. A four-question smoke test removed four more:

| Model | Smoke test |
|---|---|
| `google/gemma-4-31b-it:free` | 429 on every call ("temporarily rate-limited upstream"), here and in every earlier phase |
| `google/gemma-4-26b-a4b-it:free` | 429 on every call |
| `thinkingmachines/inkling:free` | 403: "only available on agentic harnesses" |
| `thinkingmachines/inkling-small:free` | 403, same reason |
| `nvidia/nemotron-3.5-super-vl:free` | 4/4 answered, 2.7 s median |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 4/4 answered, 11.5 s median |

`nvidia/nemotron-3.5-lightning:free` was already the working fallback from Phase 5, so it went straight to the full run.

## Bake-off

`evals/judge_eval.py` ran all 35 golden questions through the app's own retrieval and prompt for each candidate and for the chain, then had a judge grade every answer against the exact context the model saw. The models ran in parallel on 2026-11-02. Raw rows: `evals/results/bakeoff_2026-11-02.json`.

| | super-vl | lightning | ultra | chain |
|---|---|---|---|---|
| Errors (all 50 s timeouts) | 0 | 2 | 0 | **0** |
| Faithfulness: claims the context supports | 1.000 | 0.984 | 0.974 | 0.988 |
| Answers whose citations all check out | 0.81 | 0.70 | 0.69 | 0.75 |
| Golden facts in the answer | 0.79 | 0.67 | 0.79 | 0.79 |
| Unanswerable questions declined | 3/3 | 3/3 | 2/3 | 3/3 |
| Arabic questions answered in Arabic | 5/5 | 4/5 | 5/5 | 5/5 |
| Latency p50 / p95 | **3.5 s / 6.6 s** | 10.7 s / 15.5 s | 11.6 s / 27.8 s | **3.0 s / 6.3 s** |

The plan's check passes: the chain answered all 35 questions without an error. Super-vl served every one of them, so the fallbacks were never needed during the run.

### Reading the numbers

- **Latency decided the primary.** The three models are close on faithfulness, and super-vl answers three times faster than the other two. A p95 of 6.6 s against a 50 s deadline leaves room for slow moments on a free endpoint. Across both full runs (70 questions each), super-vl timed out once, ultra once and lightning twice.
- **The chain and super-vl alone differ only by noise.** Both runs were served by super-vl, yet the chain's citation score is 0.06 lower. The same chain scored 0.90 in an earlier run the same night. Treat differences under about 0.1 on citations as noise from a 35-question set graded by a sampled model.
- **The judge is strict and sometimes wrong.** It counts "visit wasalt.sa for details" as an unsupported claim. It flagged ultra's Riyadh land prices (SAR 141,000,000 and SAR 960,000,000), which are exactly what the captured Wasalt listings say. It marked ultra's correct answer to the Japan trap ("Dar Global does not have projects in Japan") as not declining. Faithfulness near 1.0 holds up; the citation scores understate the models.
- **Arabic.** A spot check of super-vl's five Arabic answers: all in Arabic, وصلت spelled correctly (lightning wrote ويزلت earlier), rents with no stated period marked "غير محددة" instead of given one, every claim cited. Faults: English words left inside Arabic sentences ("napshot", "residences") and إنفاذ misspelled as إنفاث.

### Why this order

1. **super-vl** is the fastest and the most faithful, with one timeout in 70 questions.
2. **lightning** comes second because it is the model that carried the app through Phases 5 to 7, declines what the documents cannot answer, and is smaller than ultra, so it is likelier to have free capacity when super-vl does not.
3. **ultra** comes last: good answers, but the slowest p95 (27.8 s), close to the 50 s deadline once a fallback hop is added.

## The judge

The Phase 7 runs used `nemotron-3-ultra` as the judge. Once ultra became a candidate it would have been grading its own answers, and it was slow, so the bake-off moved to a judge from a different vendor than every candidate. Five free non-NVIDIA models were probed with one planted answer: a true founding year plus an invented "500 employees", both cited to the same document.

| Judge candidate | Verdict |
|---|---|
| `dots-studio/dots-3-note-preview:free` | flagged the invented claim and the bad citation; chosen |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | flagged the claim, passed the citation |
| `cohere/north-mini-code:free` | missed the claim |
| `recursive/flint-v1.0:free`, `xingchen-agi/xing4.0-29b-a4b:free` | 503 and 429 |

The first bake-off left 21 of 140 answers ungraded: dots reasons before it answers, and the 4,000-token limit cut its verdict off. With a 16,000-token limit the rerun graded all 140.

## Risks

- **One vendor.** All three models in the chain are NVIDIA's free endpoints. A wider outage at that provider takes out every fallback at once. The two non-NVIDIA models that answer questions well (Gemma 4) returned 429 on every call from 2026-11-01 to 2026-11-02, so they could not be added. If a paid budget is ever allowed, the cheapest fix is a paid model from another vendor as the last fallback.
- **A timeout does not fall back.** OpenRouter falls back on an error, not on a slow answer. If the primary stalls, the request hits the app's 50 s deadline and the user sees the "try again" message.
- **Free models change.** Free endpoints appear, get rate-limited and disappear without notice. Rerun the bake-off before trusting the chain again:

```bash
uv run python -m evals.judge_eval \
  --models nvidia/nemotron-3.5-super-vl:free,nvidia/nemotron-3.5-lightning:free \
  --out evals/results/bakeoff_$(date +%F).json
# join ids with "+" to test a whole chain as one run: a+b+c
```
