"""Does the request text actually authorize deletion? (multilingual; the model describes)

Each distinct request text is classified once, in batches, and cached. The model returns
a request type plus a boolean; code then applies one fixed rule: only an explicit request
to delete/erase/close the account or its personal data authorizes deletion. Access,
export, correction, opt-out and "verify what happened" questions about an already-requested
deletion are distinguished explicitly, because the samples show that follow-ups like
"please confirm the profile is gone" are still deletion cases.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from .llm import LLM

SYSTEM = """You classify privacy requests sent to an online shop. Requests may be in any language.
For each numbered request decide whether it AUTHORIZES DELETION of the person's account or personal data.

authorizes_deletion = true when the person asks to delete, erase, close or remove their account/profile
or personal data, OR asks the shop to finish / verify / confirm a deletion they already requested
(e.g. "I deleted my account, please check nothing remains", "confirm my profile is gone").
authorizes_deletion = false when the person only wants: a copy/export/download of their data, access to
records or invoices, a correction or update of details (email, phone, address), a marketing or
newsletter opt-out, or anything that keeps the account active ("keep my account", "I want to continue").
If a request both asks for an export AND says to keep the account, it does NOT authorize deletion.

Return JSON only: {"items": [{"id": <number>, "request_type": "DELETION|DELETION_FOLLOW_UP|ACCESS|EXPORT|CORRECTION|MARKETING_OPT_OUT|OTHER", "authorizes_deletion": true|false, "language": "<ISO code>", "reason": "<= 15 words"}]}"""


class IntentItem(BaseModel):
    id: int
    request_type: str
    authorizes_deletion: bool
    language: str = ""
    reason: str = ""


class IntentBatch(BaseModel):
    items: list[IntentItem] = Field(default_factory=list)


def classify(cases: list[dict], llm: LLM, batch_size: int = 40) -> dict[str, dict]:
    texts = sorted({q["request_text"] for q in cases})
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]

    def run(batch):
        body = "\n".join(f"[{i}] {t}" for i, t in enumerate(batch))
        res, meta = llm.ask_json(SYSTEM, body, IntentBatch, tag="intent")
        return batch, res, meta

    by_text: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for batch, res, meta in pool.map(run, batches):
            got = {it.id: it for it in (res.items if res else [])}
            for i, t in enumerate(batch):
                it = got.get(i)
                if it is None:  # model unavailable: the safe default is to audit the deletion
                    by_text[t] = {"authorizes_deletion": True, "request_type": "UNKNOWN",
                                  "source": "default (no model answer)"}
                else:
                    by_text[t] = {**it.model_dump(exclude={"id"}), "source": meta.get("model")}
    return {q["case_id"]: by_text[q["request_text"]] for q in cases}
