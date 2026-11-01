"""Retrieval eval: hit@5 and MRR over evals/golden.yaml. Exits 1 below the gate (used in CI).

Usage: uv run python -m evals.retrieval_eval [-v]
"""

import argparse
import sys
from pathlib import Path

import yaml

from app.retrieval import load_index, search

GOLDEN = Path(__file__).with_name("golden.yaml")
HIT_AT = 5
GATE_HIT = 0.85
GATE_ARABIC = 4  # of 5


def satisfies(chunk: dict, item: dict) -> bool:
    if "listing" in item:
        meta = chunk.get("meta", {})
        return chunk["kind"] == "listing" and all(
            meta.get(k) == v for k, v in item["listing"].items()
        )
    url = chunk["url"].lower()
    return any(
        url.endswith(f[:-1].lower()) if f.endswith("$") else f.lower() in url
        for f in item["urls"]
    )


def evaluate(index, golden: list[dict]) -> dict:
    rows = []
    for item in golden:
        if item.get("answerable", True) is False:
            continue
        top = search(index, item["q"], k=HIT_AT).chunks
        rank = next((i + 1 for i, c in enumerate(top) if satisfies(c, item)), None)
        rows.append({"item": item, "rank": rank, "top": top})
    hits = [r for r in rows if r["rank"]]
    arabic = [r for r in rows if r["item"].get("lang") == "ar"]
    return {
        "rows": rows,
        "hit_at_5": len(hits) / len(rows),
        "mrr": sum(1 / r["rank"] for r in hits) / len(rows),
        "arabic_hits": sum(1 for r in arabic if r["rank"]),
        "arabic_total": len(arabic),
    }


def main(verbose: bool) -> int:
    report = evaluate(load_index(), yaml.safe_load(GOLDEN.read_text()))
    for r in report["rows"]:
        if verbose or not r["rank"]:
            label = f"@{r['rank']}" if r["rank"] else "MISS"
            print(f"{label:5} {r['item']['q']}")
            if not r["rank"]:
                for c in r["top"]:
                    print(f"        {c['kind']:7} {c['url'][-70:]}")
    ok = report["hit_at_5"] >= GATE_HIT and report["arabic_hits"] >= GATE_ARABIC
    print(
        f"\nhit@{HIT_AT} {report['hit_at_5']:.3f} (gate {GATE_HIT})  MRR {report['mrr']:.3f}  "
        f"Arabic {report['arabic_hits']}/{report['arabic_total']} (gate {GATE_ARABIC})  "
        f"{'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    sys.exit(main(parser.parse_args().verbose))
