from __future__ import annotations

import re

# Every JSON-decision AI feature in this app (entry/exit review, AI Origination,
# AI Exit Calls) asks the model for "JSON only, no markdown/code fences" -- but
# that's a request, not a guarantee. Smaller/faster models in particular are
# more prone to wrapping the object in a ```json fence, or adding a stray
# sentence before/after it, even when told not to. Previously every caller ran
# raw text straight into json.loads() and treated any failure as a generic,
# undiagnosable "Invalid AI response." -- this cleans the common cases first so
# a well-formed decision wrapped in noise doesn't get thrown away.
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> str:
    """Best-effort cleanup of an LLM's supposedly-JSON-only response before
    handing it to json.loads(). Strips markdown code fences and, failing that,
    slices out the outermost {...} span if there's leading/trailing prose
    around it. Does NOT itself validate the result -- the caller's existing
    json.loads()/except block still does that; this only improves the odds
    that well-formed JSON survives being wrapped in noise."""
    if not isinstance(text, str):
        return text
    cleaned = text.strip()
    fence_match = _FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return cleaned[first_brace : last_brace + 1]
    return cleaned
