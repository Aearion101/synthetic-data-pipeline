"""Shared pieces runner_v2 and bearer_adapter both need, in a third module so
their identity is single even when runner_v2 runs as a script: python then
executes it as __main__ AND bearer_adapter's import loads it again as
"runner_v2", producing two distinct OciAdapter.Throttled classes. A 429 raised
as one class slips past the other's except clause and is mislabeled "unknown"
(independent-review defect 1, 2026-11-11; caught only by a real CLI subprocess
fixture). Everything identity-sensitive lives here, imported once under one
module name. No other runner module imports from runner_v2 for these.
"""

class Throttled(Exception):
    """HTTP 429 from the model endpoint: the runner closes the attempt as
    "throttled" (reservation kept, counted spent until reconciliation) and may
    book a bounded retry as a NEW reserved attempt. Never a silent repeat."""

def content_text(content):
    """message.content arrives as a plain string OR a list of content parts
    ({"type": "text", "text": ...}). A list is unwrapped in order and non-text
    parts are skipped; str(list) would book a billed attempt whose reply can
    never parse. Shared by the OCI SDK adapter and the bearer-token adapter so
    both reply walks enforce the same refusal rules."""
    if content is None:
        return None
    if isinstance(content, str):
        return content if content.strip() else None
    items = content if isinstance(content, (list, tuple)) else [content]
    parts = []
    for item in items:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = item.get("text")
        else:
            text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    if not parts:
        return None
    return "".join(parts)
