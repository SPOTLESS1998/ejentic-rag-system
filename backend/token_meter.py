"""
Token accounting for the RAG pipeline — a *measurable* architecture.

Every LLM hop (query-rewrite, synthesis) is metered. We prefer the provider's
authoritative `usage` block (returned by the NVIDIA NIM OpenAI-compatible API);
when it's absent — e.g. some streaming paths don't emit usage — we fall back to
a local estimate so a number is ALWAYS recorded. The `source` field says which,
so nobody mistakes an estimate for ground truth.

No new dependencies: the estimator is a well-known ~4-chars-per-token heuristic,
refined slightly for whitespace/punctuation. It's an approximation, not a
tokenizer, and is labelled as such.
"""
from __future__ import annotations

import math
from typing import Optional


def estimate_tokens(text: str) -> int:
    """Rough token count without a tokenizer dependency.

    Uses max(chars/4, words * 0.75) — the two standard back-of-envelope rules —
    and takes the larger so we never wildly under-count. Good enough for
    budgeting and for valuing what the confidence gate saved; not billing-grade.
    """
    if not text:
        return 0
    chars = len(text)
    words = len(text.split())
    return max(1, int(math.ceil(max(chars / 4.0, words * 0.75))))


def _dig(obj, key):
    """Read `key` from a dict OR an attribute on an object; else None."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_usage(resp) -> Optional[dict]:
    """Pull {prompt_tokens, completion_tokens, total_tokens} out of a LlamaIndex
    ChatResponse if the provider reported it. Returns None if unavailable.

    LlamaIndex surfaces the raw provider payload in a few shapes depending on
    version, so we probe the common ones: resp.raw['usage'], resp.raw.usage,
    and resp.additional_kwargs (where some integrations copy the counts)."""
    candidates = []
    raw = getattr(resp, "raw", None)
    candidates.append(_dig(raw, "usage"))
    candidates.append(getattr(raw, "usage", None) if raw is not None else None)
    candidates.append(getattr(resp, "additional_kwargs", None))

    for usage in candidates:
        if not usage:
            continue
        prompt = _dig(usage, "prompt_tokens")
        completion = _dig(usage, "completion_tokens")
        total = _dig(usage, "total_tokens")
        if prompt is None and completion is None and total is None:
            continue
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = int(total) if total is not None else prompt + completion
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    return None


class TokenMeter:
    """Accumulates token usage across the LLM hops of a single query.

    `record_response()` for non-streaming hops (uses provider usage when given).
    `record_estimate()` for streaming/text-only hops. `source` collapses to
    'provider' if every hop was authoritative, 'estimate' if none were, else
    'mixed'."""

    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self._saw_provider = False
        self._saw_estimate = False

    def record_response(self, resp, *, fallback_prompt_text: str = "",
                        fallback_completion_text: str = "") -> None:
        usage = extract_usage(resp)
        if usage:
            self.prompt_tokens += usage["prompt_tokens"]
            self.completion_tokens += usage["completion_tokens"]
            self.total_tokens += usage["total_tokens"]
            self._saw_provider = True
        else:
            self.record_estimate(fallback_prompt_text, fallback_completion_text)

    def record_estimate(self, prompt_text: str = "", completion_text: str = "") -> None:
        p = estimate_tokens(prompt_text)
        c = estimate_tokens(completion_text)
        self.prompt_tokens += p
        self.completion_tokens += c
        self.total_tokens += p + c
        self._saw_estimate = True

    @property
    def source(self) -> str:
        if self._saw_provider and self._saw_estimate:
            return "mixed"
        if self._saw_provider:
            return "provider"
        if self._saw_estimate:
            return "estimate"
        return "none"

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "token_source": self.source,
        }
