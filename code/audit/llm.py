"""Thin Claude client: JSON-only answers, pydantic validation, retries, model fallback,
an on-disk response cache (so reruns are free and reproducible), and token accounting.

Fable 5.1 rejects forced tool use, so structured output is requested as plain JSON and
validated here; one repair retry includes the validation error, then we fall back.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Type

from pydantic import BaseModel, ValidationError

PRICES = {  # USD per million tokens (input, output)
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
}
# AUDIT_CACHE_DIR lets `verify.py --fresh` prove a from-scratch run gives the same output
CACHE_DIR = Path(os.environ.get("AUDIT_CACHE_DIR", Path(__file__).resolve().parent.parent / "cache"))
PROMPT_VERSION = "v1"


class Usage:
    def __init__(self):
        self.calls = defaultdict(int)
        self.cached = defaultdict(int)
        self.inp = defaultdict(int)
        self.out = defaultdict(int)

    def add(self, model: str, i: int, o: int, cached: bool):
        self.calls[model] += 1
        self.inp[model] += i
        self.out[model] += o
        if cached:
            self.cached[model] += 1

    def cost(self, model: str) -> float:
        pi, po = PRICES.get(model, (0, 0))
        return self.inp[model] / 1e6 * pi + self.out[model] / 1e6 * po

    def as_dict(self) -> dict:
        return {m: {"calls": self.calls[m], "cache_hits": self.cached[m], "input_tokens": self.inp[m],
                    "output_tokens": self.out[m], "cost_usd": round(self.cost(m), 4)} for m in self.calls}


USAGE = Usage()


class LLM:
    def __init__(self, offline: bool = False):
        self.model = os.environ.get("MODEL", "claude-fable-5-1")
        self.fallback = os.environ.get("FALLBACK_MODEL", "claude-opus-5-5")
        self.offline = offline
        self._client = None
        CACHE_DIR.mkdir(exist_ok=True)

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=4)
        return self._client

    def _key(self, model: str, system: str, content) -> str:
        blob = json.dumps([PROMPT_VERSION, model, system, content], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def ask_json(self, system: str, content: list | str, schema: Type[BaseModel], tag: str,
                 max_tokens: int = 16000) -> tuple[BaseModel | None, dict]:
        """Return (validated object or None, metadata). Never raises on model errors."""
        meta: dict = {"tag": tag}
        for model in (self.model, self.fallback):
            key = self._key(model, system, content)
            path = CACHE_DIR / f"{tag}_{key}.json"
            if path.exists():
                rec = json.loads(path.read_text(encoding="utf-8"))
                USAGE.add(model, rec["usage"]["input"], rec["usage"]["output"], cached=True)
                try:
                    return schema.model_validate(rec["parsed"]), {**meta, "model": model, "cache": "hit"}
                except ValidationError:
                    pass
            if self.offline:
                continue
            messages = [{"role": "user", "content": content}]
            for _attempt in range(2):
                try:
                    r = self.client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                                    messages=messages)
                except Exception as e:  # overload, refusal, network: try the fallback model
                    meta.setdefault("errors", []).append(f"{model}: {type(e).__name__}: {str(e)[:160]}")
                    time.sleep(2)
                    break
                text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
                USAGE.add(model, r.usage.input_tokens, r.usage.output_tokens, cached=False)
                if r.stop_reason == "refusal":
                    meta.setdefault("errors", []).append(f"{model}: refusal")
                    break
                try:
                    obj = schema.model_validate(_extract_json(text))
                except (ValidationError, ValueError) as e:
                    messages = messages + [{"role": "assistant", "content": text or "{}"},
                                           {"role": "user", "content": f"That was not valid JSON for the schema: {str(e)[:500]}. Reply with corrected JSON only."}]
                    continue
                path.write_text(json.dumps({"model": model, "parsed": obj.model_dump(),
                                            "usage": {"input": r.usage.input_tokens, "output": r.usage.output_tokens}},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
                return obj, {**meta, "model": model, "cache": "miss"}
        return None, {**meta, "model": None}


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON found")
    return json.loads(text[start:text.rfind("}" if text[start] == "{" else "]") + 1])
