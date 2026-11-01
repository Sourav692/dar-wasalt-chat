"""LLM-judge eval over evals/golden.yaml: faithfulness, citations, fact recall, latency.

Runs the real pipeline per model, then asks a judge model to grade each answer against the
context the model was given. With several --models it is the model bake-off.

Usage: uv run python -m evals.judge_eval [--models a,b] [--limit N] [--out results.json]
Needs OPENROUTER_API_KEY and the built index. Not run in CI (live model calls).
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import yaml

from app.config import get_settings
from app.retrieval import load_index
from evals.common import JUDGE_MODEL, complete, generate, parse_json, percentile

GOLDEN = Path(__file__).with_name("golden.yaml")
CONCURRENCY = 2  # free models allow ~20 requests a minute

JUDGE_PROMPT = """You are grading one answer from a retrieval-augmented assistant that must \
answer only from the documents in <context>.

{context}

Question: {question}

Answer to grade:
<answer>
{answer}
</answer>

Grade strictly against the context, not your own knowledge. Reply with only a JSON object:
{{"claims": <number of factual claims in the answer>,
 "supported": <number of those claims the context supports>,
 "citations_ok": <true if each cited [n] supports the claim it is attached to and no factual \
claim is left uncited, else false>,
 "declined": <true if the answer says the information is not available or declines>,
 "same_language": <true if the answer is in the language of the question>,
 "unsupported": [<each unsupported claim, quoted briefly>]}}"""


async def grade(http, row: dict, item: dict) -> dict:
    if row["error"]:
        return row | {"grade": None}
    prompt = JUDGE_PROMPT.format(
        context=row["context"].rsplit("\n\nQuestion:", 1)[0],
        question=row["question"],
        answer=row["answer"],
    )
    try:
        verdict = parse_json(await complete(http, JUDGE_MODEL, prompt))
    except Exception as e:  # noqa: BLE001 - a judge failure is recorded, not fatal
        return row | {"grade": None, "judge_error": str(e)[:200]}
    facts = item.get("facts", [])
    hits = [f for f in map(str, facts) if f.lower() in row["answer"].lower()]
    return row | {"grade": verdict, "fact_recall": len(hits) / len(facts) if facts else None}


async def run_model(index, http, golden: list[dict], model: str) -> list[dict]:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(item):
        async with gate:
            row = await generate(index, http, item["q"], model)
            return await grade(http, row, item) | {
                "answerable": item.get("answerable", True),
                "lang": item.get("lang", "en"),
            }

    return await asyncio.gather(*(one(item) for item in golden))


def summarize(model: str, rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get("grade")]
    answerable = [r for r in graded if r["answerable"]]
    unanswerable = [r for r in graded if not r["answerable"]]
    claims = sum(r["grade"].get("claims", 0) for r in answerable)
    supported = sum(r["grade"].get("supported", 0) for r in answerable)
    recalls = [r["fact_recall"] for r in answerable if r.get("fact_recall") is not None]
    arabic = [r for r in graded if r["lang"] == "ar"]
    latencies = [r["latency_s"] for r in rows if not r["error"]]
    return {
        "model": model,
        "questions": len(rows),
        "errors": sum(1 for r in rows if r["error"]),
        "judge_failures": sum(1 for r in rows if not r["error"] and not r.get("grade")),
        "faithfulness": round(supported / claims, 3) if claims else None,
        "citations_ok": round(
            sum(1 for r in answerable if r["grade"].get("citations_ok")) / len(answerable), 3
        )
        if answerable
        else None,
        "fact_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "declined_unanswerable": f"{sum(1 for r in unanswerable if r['grade'].get('declined'))}"
        f"/{len(unanswerable)}",
        "arabic_same_language": f"{sum(1 for r in arabic if r['grade'].get('same_language'))}"
        f"/{len(arabic)}",
        "p50_s": percentile(latencies, 0.5),
        "p95_s": percentile(latencies, 0.95),
    }


async def main(models: list[str], limit: int | None, out: Path | None) -> None:
    golden = yaml.safe_load(GOLDEN.read_text())[:limit]
    index = load_index()
    report = {"judge": JUDGE_MODEL, "summaries": [], "rows": {}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as http:
        for model in models:
            rows = await run_model(index, http, golden, model)
            summary = summarize(model, rows)
            report["summaries"].append(summary)
            report["rows"][model] = [{k: v for k, v in r.items() if k != "context"} for r in rows]
            print(json.dumps(summary, ensure_ascii=False), flush=True)
    if out:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"-> {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", help="comma-separated; default: the configured chain's first")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    chosen = args.models.split(",") if args.models else get_settings().models[:1]
    asyncio.run(main(chosen, args.limit, args.out))
