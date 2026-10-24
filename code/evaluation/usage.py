"""Write evaluation/usage_report.md from the token accounting of the run that produced output.csv."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from audit.llm import PRICES, USAGE


def write_usage_report(path: Path, n_requests: int, split: str, offline: bool) -> None:
    u = USAGE.as_dict()
    lines = [
        "# Token usage and cost — final full-dataset run",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')} by `python code/main.py`"
        f"{' --offline' if offline else ''} (split: `{split}`, {n_requests} requests).",
        "- Provider: **Anthropic (Claude API)**. Prices used for estimates (USD per million tokens, input / output): "
        + ", ".join(f"`{m}` {p[0]:.2f} / {p[1]:.2f}" for m, p in PRICES.items()) + ".",
        "- Model calls happen only for (a) reading the 11 screenshots + 4 PDFs once and (b) classifying each",
        "  *distinct* request text once (batched 40 per call). Everything else is deterministic Python, so the",
        "  per-request cost is the amortized cost of those calls. Every answer is cached in `code/cache/`;",
        "  `cache hits` below are calls served from that cache (no tokens billed in this run).",
        "",
        "| Model | API calls | cache hits | input tokens | output tokens | total tokens | est. cost (USD) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot = {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for m, d in u.items():
        billed_calls = d["calls"] - d["cache_hits"]
        lines.append(f"| `{m}` | {billed_calls} | {d['cache_hits']} | {d['input_tokens']:,} | {d['output_tokens']:,} | "
                     f"{d['input_tokens'] + d['output_tokens']:,} | {d['cost_usd']:.4f} |")
        for k in tot:
            tot[k] += d[k]
    total_tokens = tot["input_tokens"] + tot["output_tokens"]
    lines += [
        f"| **Overall** | {tot['calls'] - tot['cache_hits']} | {tot['cache_hits']} | {tot['input_tokens']:,} | "
        f"{tot['output_tokens']:,} | {total_tokens:,} | {tot['cost_usd']:.4f} |",
        "",
        "Token counts include calls served from cache (the tokens those answers originally cost), so the",
        "figures describe the full cost of producing this output, not only what was billed in this run.",
        "",
        "| Per request (÷ %d) | value |" % n_requests,
        "|---|---:|",
        f"| average total tokens | {total_tokens / n_requests:,.1f} |",
        f"| average input tokens | {tot['input_tokens'] / n_requests:,.1f} |",
        f"| average output tokens | {tot['output_tokens'] / n_requests:,.1f} |",
        f"| average model calls | {tot['calls'] / n_requests:.3f} |",
        f"| estimated cost | ${tot['cost_usd'] / n_requests:.5f} |",
        "",
        "Why it is cheap: the model *describes* (artifact facts, request intent) once per distinct input;",
        "deterministic code *decides* per case. A naive agent-per-case design would re-read evidence for",
        "every one of the 518 requests.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
